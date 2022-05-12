"""
This module provides the communication with the SPARQL endpoint to provide
data for the API server.
"""

import secrets
import os.path
import logging
from datetime import datetime
from urllib.error import URLError
from SPARQLWrapper import SPARQLWrapper, JSON, SPARQLExceptions
from rdflib import Graph, Literal, RDF, XSD
from jinja2 import Environment, FileSystemLoader
from djehuty.web import cache
from djehuty.utils import counters
from djehuty.utils import rdf
from djehuty.utils import convenience as conv

class UnknownDatabaseState(Exception):
    """Raised when the database is not queryable."""

class EmptyDatabase(Exception):
    """Raised when the database is empty."""

class SparqlInterface:
    """This class reads and writes data from a SPARQL endpoint."""

    def __init__ (self):

        self.ids         = counters.IdGenerator()
        self.storage     = None
        self.endpoint    = "http://127.0.0.1:8890/sparql"
        self.state_graph = "https://data.4tu.nl/portal/self-test"
        self.privileges  = {}
        self.cache       = cache.CacheLayer(None)
        self.jinja       = Environment(loader = FileSystemLoader(
                            os.path.join(os.path.dirname(__file__),
                                         "resources/sparql_templates")),
                                       # Auto-escape is set to False because
                                       # we put quotes around strings in
                                       # filters.
                                       autoescape=False)

        self.sparql      = SPARQLWrapper(self.endpoint)
        self.sparql.setReturnFormat(JSON)
        self.sparql_is_up = True

    def load_state (self):
        """Procedure to load the database state."""

        # Set the iterators to continue where we left off last time the
        # program was run.
        try:
            for item in self.ids.keys():
                self.ids.set_id (self.__highest_id (item_type=item), item)
                if self.ids.current_id (item) is None:
                    raise UnknownDatabaseState

        except EmptyDatabase:
            logging.warning ("It looks like the database is empty.")

        if not os.environ.get('WERKZEUG_RUN_MAIN') and self.sparql_is_up:
            for item in self.ids.keys():
                logging.info ("%s enumerator set to %d", item, self.ids.current_id (item))

    ## ------------------------------------------------------------------------
    ## Private methods
    ## ------------------------------------------------------------------------

    def __log_query (self, query):
        logging.info ("Query:\n---\n%s\n---", query)

    def __normalize_binding (self, record):
        for item in record:
            if record[item]["type"] == "typed-literal":
                datatype = record[item]["datatype"]
                if datatype == "http://www.w3.org/2001/XMLSchema#integer":
                    record[item] = int(float(record[item]["value"]))
                elif datatype == "http://www.w3.org/2001/XMLSchema#decimal":
                    record[item] = int(float(record[item]["value"]))
                elif datatype == "http://www.w3.org/2001/XMLSchema#boolean":
                    record[item] = bool(int(record[item]["value"]))
                elif datatype == "http://www.w3.org/2001/XMLSchema#dateTime":
                    time_value = record[item]["value"]
                    if time_value[-1] == 'Z':
                        time_value = time_value[:-1]
                    timestamp    = datetime.strptime(time_value, "%Y-%m-%dT%H:%M:%S")
                    record[item] = datetime.strftime (timestamp, "%Y-%m-%d %H:%M:%S")
                elif datatype == "http://www.w3.org/2001/XMLSchema#string":
                    if record[item]["value"] == "NULL":
                        record[item] = None
                    else:
                        record[item] = record[item]["value"]
            elif record[item]["type"] == "literal":
                if (record[item]['value'].startswith("Modify ") or
                    record[item]['value'].startswith("Insert into ") or
                    record[item]['value'].startswith("Delete from ")):
                    logging.info("RDF store: %s", record[item]['value'])
                else:
                    logging.info("Literal: %s", record[item]['value'])

                return record[item]["value"]
            elif record[item]["type"] == "uri":
                record[item] = str(record[item]["value"])
            else:
                logging.info("Not a typed-literal: %s", record[item]['type'])
        return record

    def __query_from_template (self, name, args=None):
        template   = self.jinja.get_template (f"{name}.sparql")
        parameters = { "state_graph": self.state_graph }
        if args is None:
            args = {}

        return template.render ({ **args, **parameters })

    def __run_query (self, query, cache_key_string=None, prefix=None):

        cache_key = None
        if cache_key_string is not None:
            cache_key = self.cache.make_key (cache_key_string)
            cached    = self.cache.cached_value(prefix, cache_key)
            if cached is not None:
                return cached

        self.sparql.method = 'POST'
        self.sparql.setQuery(query)
        results = []
        try:
            query_results = self.sparql.query().convert()
            results = list(map(self.__normalize_binding,
                               query_results["results"]["bindings"]))

            if cache_key_string is not None:
                self.cache.cache_value (prefix, cache_key, results)

            if not self.sparql_is_up:
                logging.info("Connection to the SPARQL endpoint seems up again.")
                self.sparql_is_up = True

        except URLError:
            if self.sparql_is_up:
                logging.error("Connection to the SPARQL endpoint seems down.")
                self.sparql_is_up = False
        except SPARQLExceptions.QueryBadFormed:
            logging.error("Badly formed SPARQL query:")
            self.__log_query (query)
        except Exception as error:
            logging.error("SPARQL query failed.")
            logging.error("Exception: %s", error)
            self.__log_query (query)

        return results

    def __highest_id (self, item_type="article"):
        """Return the highest numeric ID for ITEM_TYPE."""
        prefix = conv.to_camel(item_type)
        query = self.__query_from_template ("highest_id", {
            "item_type":   prefix
        })

        try:
            results = self.__run_query (query)
            return results[0]["id"]
        except IndexError:
            return 0
        except KeyError:
            return None

    def __insert_query_for_graph (self, graph):
        return rdf.insert_query (self.state_graph, graph)

    ## ------------------------------------------------------------------------
    ## GET METHODS
    ## ------------------------------------------------------------------------

    def dataset_storage_used (self, container_uri):
        """Returns the number of bytes used by an article."""

        query = self.__query_from_template ("dataset_storage_used", {
            "container_uri": container_uri
        })

        results = self.__run_query (query, query, f"{container_uri}_dataset")
        try:
            return results[0]["bytes"]
        except IndexError:
            pass
        except KeyError:
            pass

        return 0

    def dataset_versions (self, limit=1000, offset=0, order="version",
                          order_direction="desc", article_id=None,
                          container_uri=None):
        """Procedure to retrieve the versions of an article."""
        filters  = rdf.sparql_filter ("article_id", article_id)
        filters += rdf.sparql_filter ("container_uri", container_uri, is_uri=True)

        query = self.__query_from_template ("dataset_versions", {
            "filters":     filters
        })
        query += rdf.sparql_suffix (order, order_direction, limit, offset)

        return self.__run_query (query)

    def datasets (self, account_id=None, categories=None, collection_uri=None,
                  container_uuid=None, dataset_id=None, dataset_uuid=None, doi=None,
                  exclude_ids=None, groups=None, handle=None, institution=None,
                  is_latest=False, item_type=None, limit=None, modified_since=None,
                  offset=None, order=None, order_direction=None, published_since=None,
                  resource_doi=None, return_count=False, search_for=None,
                  version=None, is_published=True):
        """Procedure to retrieve version(s) of datasets."""

        filters  = rdf.sparql_filter ("container_uri",  rdf.uuid_to_uri (container_uuid, "container"), is_uri=True)
        filters  = rdf.sparql_filter ("article",        rdf.uuid_to_uri (dataset_uuid, "article"), is_uri=True)
        filters  = rdf.sparql_filter ("institution_id", institution)
        filters += rdf.sparql_filter ("defined_type",   item_type)
        filters += rdf.sparql_filter ("article_id",     dataset_id)
        filters += rdf.sparql_filter ("version",        version)
        filters += rdf.sparql_filter ("resource_doi",   resource_doi, escape=True)
        filters += rdf.sparql_filter ("doi",            doi,          escape=True)
        filters += rdf.sparql_filter ("handle",         handle,       escape=True)
        filters += rdf.sparql_in_filter ("group_id",    groups)
        filters += rdf.sparql_in_filter ("article_id", exclude_ids, negate=True)

        if categories is not None:
            filters += f"FILTER ((?category_id IN ({','.join(map(str, categories))})) OR "
            filters += f"(?parent_category_id IN ({','.join(map(str, categories))})))\n"

        if search_for is not None:
            filters += f"FILTER (CONTAINS(STR(?title),          \"{search_for}\") OR\n"
            filters += f"        CONTAINS(STR(?resource_title), \"{search_for}\") OR\n"
            filters += f"        CONTAINS(STR(?description),    \"{search_for}\") OR\n"
            filters += f"        CONTAINS(STR(?citation),       \"{search_for}\"))"

        if published_since is not None:
            filters += rdf.sparql_bound_filter ("published_date")
            filters += f"FILTER (?published_date > \"{published_since}\"^^xsd:dateTime)\n"

        if modified_since is not None:
            filters += rdf.sparql_bound_filter ("modified_date")
            filters += f"FILTER (?modified_date > \"{modified_since}\"^^xsd:dateTime)\n"

        query = self.__query_from_template ("datasets", {
            "categories":     categories,
            "collection_uri": collection_uri,
            "account_id":     account_id,
            "is_latest":      is_latest,
            "is_published":   is_published,
            "filters":        filters,
            "return_count":   return_count
        })

        # Setting the default value for 'limit' to 10 makes passing
        # parameters from HTTP requests cumbersome. Therefore, we
        # set the default again here.
        if limit is None:
            limit = 10

        if not return_count:
            query += rdf.sparql_suffix (order, order_direction, limit, offset)

        return self.__run_query (query, query, "datasets")

    def repository_statistics (self):
        """Procedure to retrieve repository-wide statistics."""

        parameters        = { "state_graph":   self.state_graph }
        articles_query    = self.__query_from_template ("statistics_datasets", parameters)
        collections_query = self.__query_from_template ("statistics_collections", parameters)
        authors_query     = self.__query_from_template ("statistics_authors", parameters)
        files_query       = self.__query_from_template ("statistics_files", parameters)

        row = { "articles": 0, "authors": 0, "collections": 0, "files": 0, "bytes": 0 }
        try:
            articles    = self.__run_query (articles_query, articles_query, "statistics")
            authors     = self.__run_query (authors_query, authors_query, "statistics")
            collections = self.__run_query (collections_query, collections_query, "statistics")
            files       = self.__run_query (files_query, files_query, "statistics")
            number_of_files = 0
            number_of_bytes = 0
            for entry in files:
                number_of_files += 1
                number_of_bytes += int(float(entry["bytes"]))

            files_results = {
                "files": number_of_files,
                "bytes": number_of_bytes
            }
            row = { **articles[0], **authors[0], **collections[0], **files_results }
        except IndexError:
            pass
        except KeyError:
            pass

        return row

    def article_statistics (self, item_type="downloads",
                                  order="downloads",
                                  order_direction="desc",
                                  group_ids=None,
                                  category_ids=None,
                                  limit=10,
                                  offset=0):
        """Procedure to retrieve article statistics."""

        prefix  = item_type.capitalize()
        filters = ""

        if category_ids is not None:
            filters += f"FILTER ((?category_id) IN ({','.join(map(str, category_ids))}))\n"

        if group_ids is not None:
            filters += f"FILTER ((?group_id) IN ({','.join(map(str, group_ids))}))\n"


        query   = self.__query_from_template ("article_statistics", {
            "category_ids":  category_ids,
            "item_type":     item_type,
            "prefix":        prefix,
            "filters":       filters
        })

        query += rdf.sparql_suffix (order, order_direction, limit, offset)
        return self.__run_query (query, query, "statistics")

    def article_statistics_timeline (self,
                                     article_id=None,
                                     item_type="downloads",
                                     order="downloads",
                                     order_direction="desc",
                                     category_ids=None,
                                     limit=10,
                                     offset=0,
                                     aggregation_type="day"):
        """Procedure to retrieve article statistics per date."""

        item_class  = item_type.capitalize()
        filters = ""

        if article_id is not None:
            filters += rdf.sparql_filter("article_id", article_id)

        if category_ids is not None:
            filters += f"FILTER (?category_id={category_ids[0]}"
            for category_id in category_ids[1:]:
                filters += f" OR ?category_id={category_id}"
            filters += ")\n"

        query   = self.__query_from_template ("article_statistics_timeline", {
            "category_ids":  category_ids,
            "item_type":     item_type,
            "item_class":    item_class,
            "filters":       filters
        })

        order = "article_id" if order is None else order
        query += rdf.sparql_suffix (order, order_direction, limit, offset)
        return self.__run_query (query, query, "statistics")

    def single_article_statistics_totals (self, article_id):
        """Procedure to get shallow statistics of an article."""

        query   = self.__query_from_template ("single_article_statistics_totals", {
            "article_id":   article_id
        })

        return self.__run_query (query, query, "statistics")

    def authors (self, first_name=None, full_name=None, group_id=None,
                 author_id=None, institution_id=None, is_active=None,
                 is_public=None, job_title=None, last_name=None,
                 orcid_id=None, url_name=None, limit=10, order="order_index",
                 order_direction="asc", item_uri=None, search_for=None,
                 account_id=None, item_type="article"):
        """Procedure to retrieve authors of an article."""

        prefix = item_type.capitalize()

        filters  = rdf.sparql_filter ("group_id",       group_id)
        filters += rdf.sparql_filter ("id",             author_id)
        filters += rdf.sparql_filter ("institution_id", institution_id)
        filters += rdf.sparql_filter ("is_active",      is_active)
        filters += rdf.sparql_filter ("is_public",      is_public)
        filters += rdf.sparql_filter ("job_title",      job_title,  escape=True)
        filters += rdf.sparql_filter ("first_name",     first_name, escape=True)
        filters += rdf.sparql_filter ("last_name",      last_name,  escape=True)
        filters += rdf.sparql_filter ("full_name",      full_name,  escape=True)
        filters += rdf.sparql_filter ("orcid_id",       orcid_id,   escape=True)
        filters += rdf.sparql_filter ("url_name",       url_name,   escape=True)

        if search_for is not None:
            filters += (f"FILTER (CONTAINS(STR(?first_name), \"{search_for}\") OR\n"
                        f"        CONTAINS(STR(?last_name),  \"{search_for}\") OR\n"
                        f"        CONTAINS(STR(?full_name),  \"{search_for}\") OR\n"
                        f"        CONTAINS(STR(?orcid_id),   \"{search_for}\"))")

        query = self.__query_from_template ("authors", {
            "item_type":   item_type,
            "prefix":      prefix,
            "item_uri":    item_uri,
            "account_id":  account_id,
            "filters":     filters
        })
        query += rdf.sparql_suffix (order, order_direction, limit, None)

        return self.__run_query(query)

    def file_by_id (self, file_id, account_id):
        """Procedure to get a file by its identifier."""

        if file_id is None or account_id is None:
            return None

        query = self.__query_from_template ("file", {
            "account_id":  account_id,
            "file_id":     file_id
        })

        return self.__run_query(query)

    def article_files (self, name=None, size=None, is_link_only=None,
                       file_id=None, download_url=None, supplied_md5=None,
                       computed_md5=None, viewer_type=None, preview_state=None,
                       status=None, upload_url=None, upload_token=None,
                       order="order_index", order_direction="asc", limit=10,
                       article_uri=None, account_id=None):
        """Procedure to retrieve files of an article."""

        filters  = rdf.sparql_filter ("size",          size)
        filters += rdf.sparql_filter ("is_link_only",  is_link_only)
        filters += rdf.sparql_filter ("id",            file_id)
        filters += rdf.sparql_filter ("name",          name,          escape=True)
        filters += rdf.sparql_filter ("download_url",  download_url,  escape=True)
        filters += rdf.sparql_filter ("supplied_md5",  supplied_md5,  escape=True)
        filters += rdf.sparql_filter ("computed_md5",  computed_md5,  escape=True)
        filters += rdf.sparql_filter ("viewer_type",   viewer_type,   escape=True)
        filters += rdf.sparql_filter ("preview_state", preview_state, escape=True)
        filters += rdf.sparql_filter ("status",        status,        escape=True)
        filters += rdf.sparql_filter ("upload_url",    upload_url,    escape=True)
        filters += rdf.sparql_filter ("upload_token",  upload_token,  escape=True)

        query = self.__query_from_template ("article_files", {
            "article_uri":         article_uri,
            "account_id":          account_id,
            "filters":             filters
        })

        query += rdf.sparql_suffix (order, order_direction, limit, None)

        return self.__run_query(query)

    def article_references (self, reference_id=None, url=None, order=None,
                            order_direction=None, limit=10,
                            article_version_id=None, account_id=None):
        """Procedure to retrieve article references."""

        filters  = rdf.sparql_filter ("id",            reference_id)
        filters += rdf.sparql_filter ("url",           url,  escape=True)

        query = self.__query_from_template ("article_references", {
            "article_version_id":  article_version_id,
            "account_id":  account_id,
            "filters":     filters
        })

        query += rdf.sparql_suffix (order, order_direction, limit, None)

        return self.__run_query(query)

    def derived_from (self, item_uri, item_type='article',
                      order=None, order_direction=None, limit=10):
        """Procedure to retrieve derived_from links"""

        query = self.__query_from_template ("derived_from", {
            "prefix":      item_type.capitalize(),
            "item_uri":    item_uri
        })

        query += rdf.sparql_suffix (order, order_direction, limit, None)

        return [d['derived_from'] for d in self.__run_query(query)]

    def delete_article_reference (self, article_version_id, account_id, url=None):
        """Procedure to delete an article reference."""

        query = self.__query_from_template ("delete_article_reference", {
            "account_id":  account_id,
            "article_version_id": article_version_id,
            "url":         url.replace('"', '\\"')
        })

        return self.__run_query(query)

    def custom_fields (self, name=None, value=None, default_value=None,
                       field_id=None, placeholder=None, max_length=None,
                       min_length=None, field_type=None, is_multiple=None,
                       is_mandatory=None, order="name", order_direction=None,
                       limit=10, item_uri=None, item_type="article"):
        """Procedure to get custom metadata of an article or a collection."""

        prefix = item_type.capitalize()

        filters = rdf.sparql_filter ("id",            field_id)
        filters += rdf.sparql_filter ("max_length",    max_length)
        filters += rdf.sparql_filter ("min_length",    min_length)
        filters += rdf.sparql_filter ("is_multiple",   is_multiple)
        filters += rdf.sparql_filter ("is_mandatory",  is_mandatory)
        filters += rdf.sparql_filter ("name",          name,          escape=True)
        filters += rdf.sparql_filter ("value",         value,         escape=True)
        filters += rdf.sparql_filter ("default_value", default_value, escape=True)
        filters += rdf.sparql_filter ("placeholder",   placeholder,   escape=True)
        filters += rdf.sparql_filter ("field_type",    field_type,    escape=True)

        query = self.__query_from_template ("custom_fields", {
            "item_uri":    item_uri,
            "item_type":   item_type,
            "prefix":      prefix,
            "filters":     filters
        })
        query += rdf.sparql_suffix (order, order_direction, limit, None)

        return self.__run_query(query)

    def article_embargo_options (self, ip_name=None, embargo_type=None,
                                 order=None, order_direction=None,
                                 limit=10, article_version_id=None):
        """Procedure to retrieve embargo options of an article."""

        filters  = rdf.sparql_filter ("article_version_id", article_version_id)
        filters += rdf.sparql_filter ("ip_name",      ip_name,      escape=True)
        filters += rdf.sparql_filter ("embargo_type", embargo_type, escape=True)

        query = self.__query_from_template ("article_embargo_options", {
            "filters":     filters
        })
        query += rdf.sparql_suffix (order, order_direction, limit, None)

        return self.__run_query(query)

    def tags (self, order=None, order_direction=None, limit=10,
              item_uri=None, item_type="article"):
        """Procedure to get tags for an article or a collection."""

        prefix  = item_type.capitalize()
        query   = self.__query_from_template ("tags", {
            "prefix":      prefix,
            "item_type":   item_type,
            "item_uri":    item_uri
        })
        query += rdf.sparql_suffix (order, order_direction, limit, None)

        return self.__run_query(query)

    def categories (self, title=None, order=None, order_direction=None,
                    limit=10, item_uri=None, account_id=None,
                    item_type="article", is_published=True):
        """Procedure to retrieve categories of an article."""

        prefix  = item_type.capitalize()
        filters = rdf.sparql_filter ("title", title, escape=True)
        query   = self.__query_from_template ("categories", {
            "prefix":       prefix,
            "item_type":    item_type,
            "item_uri":     item_uri,
            "account_id":   account_id,
            "is_published": is_published,
            "filters":      filters
        })
        query += rdf.sparql_suffix (order, order_direction, limit, None)

        return self.__run_query(query)

    def account_categories (self, account_id, title=None, order=None,
                            order_direction=None, limit=10):
        """Procedure to retrieve categories of an article."""

        filters = rdf.sparql_filter ("title", title, escape=True)
        query   = self.__query_from_template ("account_categories", {
            "account_id":  account_id,
            "filters":     filters
        })
        query += rdf.sparql_suffix (order, order_direction, limit, None)

        return self.__run_query (query)

    def private_links (self, item_id=None, item_type="article",
                       account_id=None, id_string=None):
        """Procedure to get private links to an article or a collection."""

        prefix  = item_type.capitalize()
        query   = self.__query_from_template ("private_links", {
            "prefix":      prefix,
            "id_string":   id_string,
            "item_type":   item_type,
            "item_id":     item_id,
            "account_id":  account_id,
        })

        return self.__run_query(query)

    def licenses (self):
        """Procedure to get a list of allowed licenses."""

        query = self.__query_from_template ("licenses")

        return self.__run_query(query)

    def latest_articles_portal (self, page_size=30):
        """Procedure to get the latest articles."""

        query = self.__query_from_template ("latest_articles_portal", {
            "page_size":   page_size
        })

        return self.__run_query(query)

    def collections_from_article (self, article_id):
        """Procedure to get the collections an article is part of."""

        query = self.__query_from_template ("collections_from_article", {
            "article_id":  article_id
        })

        return self.__run_query(query)

    ## ------------------------------------------------------------------------
    ## COLLECTIONS
    ## ------------------------------------------------------------------------

    def collection_versions (self, limit=1000, offset=0, order=None,
                             order_direction=None, collection_id=None):
        """Procedure to retrieve the versions of an collection."""
        filters = ""
        if collection_id is not None:
            filters += rdf.sparql_filter ("collection_id", collection_id)

        query = self.__query_from_template ("collection_versions", {
            "filters":     filters
        })
        query += rdf.sparql_suffix (order, order_direction, limit, offset)
        return self.__run_query (query)

    ## This procedure exists because the 'articles' procedure will only
    ## count articles that are either public, or were published using the
    ## same account_id as the collection.
    ##
    ## So to get the actual count, this separate procedure exists.
    def collections_article_count (self, collection_version_id):
        """Procedure to count the articles in a collection."""

        if collection_version_id is None:
            return 0

        query = self.__query_from_template ("collection_articles_count", {
            "collection_version_id":  collection_version_id
        })
        results = self.__run_query (query)

        try:
            return results[0]["articles"]
        except KeyError:
            return 0

    def collections (self, limit=10, offset=None, order=None,
                     order_direction=None, institution=None,
                     published_since=None, modified_since=None, group=None,
                     resource_doi=None, resource_id=None, doi=None, handle=None,
                     account_id=None, search_for=None, collection_id=None,
                     collection_version_id=None, version=None,
                     is_editable=None, is_latest=None):
        """Procedure to retrieve collections."""

        filters  = rdf.sparql_filter ("institution_id", institution)
        filters += rdf.sparql_filter ("group_id",       group)
        filters += rdf.sparql_filter ("collection_id",  collection_id)
        filters += rdf.sparql_filter ("collection_version_id",  collection_version_id)
        filters += rdf.sparql_filter ("version",        version)
        filters += rdf.sparql_filter ("resource_doi",   resource_doi, escape=True)
        filters += rdf.sparql_filter ("resource_id",    resource_id,  escape=True)
        filters += rdf.sparql_filter ("doi",            doi,          escape=True)
        filters += rdf.sparql_filter ("handle",         handle,       escape=True)

        if search_for is not None:
            filters += (f"FILTER (CONTAINS(STR(?title),          \"{search_for}\") OR\n"
                        f"        CONTAINS(STR(?resource_title), \"{search_for}\") OR\n"
                        f"        CONTAINS(STR(?description),    \"{search_for}\") OR\n"
                        f"        CONTAINS(STR(?citation),       \"{search_for}\"))")

        if published_since is not None:
            filters += rdf.sparql_bound_filter ("published_date")
            filters += "FILTER (STR(?published_date) != \"NULL\")\n"
            filters += f"FILTER (STR(?published_date) > \"{published_since}\")\n"

        if modified_since is not None:
            filters += rdf.sparql_bound_filter ("modified_date")
            filters += "FILTER (STR(?modified_date) != \"NULL\")\n"
            filters += f"FILTER (STR(?modified_date) > \"{modified_since}\")\n"

        if account_id is None:
            filters += rdf.sparql_filter ("is_public", 1)
        else:
            filters += rdf.sparql_filter ("account_id", account_id)

        if is_editable is not None:
            filters += rdf.sparql_filter ("is_editable", is_editable)

        if is_latest is not None:
            filters += rdf.sparql_filter ("is_latest", is_latest)

        query   = self.__query_from_template ("collections", {
            "filters":     filters
        })
        query += rdf.sparql_suffix (order, order_direction, limit, offset)

        return self.__run_query(query)

    def fundings (self, title=None, order=None, order_direction=None,
                  limit=10, item_uri=None, account_id=None,
                  item_type="article", is_published=True):
        """Procedure to retrieve funding information."""

        filters = rdf.sparql_filter ("title", title, escape=True)
        query   = self.__query_from_template ("funding", {
            "prefix":      item_type.capitalize(),
            "item_uri":    item_uri,
            "account_id":  account_id,
            "filters":     filters
        })
        query += rdf.sparql_suffix (order, order_direction, limit, None)

        return self.__run_query(query)

    def references (self, order=None, order_direction=None, limit=10,
                    item_uri=None, account_id=None, item_type="article",
                    is_published=True):
        """Procedure to retrieve references."""

        query   = self.__query_from_template ("references", {
            "prefix":       item_type.capitalize(),
            "item_uri":     item_uri,
            "is_published": is_published,
            "account_id":   account_id,
            "filters":      None
        })
        query += rdf.sparql_suffix (order, order_direction, limit, None)

        return self.__run_query(query)

    ## ------------------------------------------------------------------------
    ## INSERT METHODS
    ## ------------------------------------------------------------------------

    def insert_article (self, title,
                        account_id,
                        article_id=None,
                        article_version_id=None,
                        description=None,
                        keywords=None,
                        defined_type=None,
                        funding=None,
                        license_id=None,
                        doi=None,
                        handle=None,
                        resource_doi=None,
                        resource_title=None,
                        first_online=None,
                        publisher_publication=None,
                        publisher_acceptance=None,
                        submission=None,
                        posted=None,
                        revision=None,
                        group_id=None,
                        funding_list=None,
                        tags=None,
                        references=None,
                        categories=None,
                        authors=None,
                        custom_fields=None,
                        private_links=None,
                        files=None,
                        embargo_options=None):
        """Procedure to insert an article to the state graph."""

        funding_list    = [] if funding_list    is None else funding_list
        tags            = [] if tags            is None else tags
        references      = [] if references      is None else references
        categories      = [] if categories      is None else categories
        authors         = [] if authors         is None else authors
        custom_fields   = [] if custom_fields   is None else custom_fields
        private_links   = [] if private_links   is None else private_links
        files           = [] if files           is None else files
        embargo_options = [] if embargo_options is None else embargo_options

        graph = Graph()

        if article_version_id is None:
            article_version_id = self.ids.next_id("article")

        if article_id is None:
            article_id = article_version_id

        article_uri = rdf.ROW[f"article_{article_version_id}"]

        ## TIMELINE
        ## --------------------------------------------------------------------
        timeline_id = self.insert_timeline (
            revision              = revision,
            first_online          = first_online,
            publisher_publication = publisher_publication,
            publisher_acceptance  = publisher_acceptance,
            posted                = posted,
            submission            = submission
        )

        rdf.add (graph, article_uri, rdf.COL["timeline_id"], timeline_id)

        ## REFERENCES
        ## --------------------------------------------------------------------
        for url in references:
            self.insert_reference (url, item_id=article_version_id, item_type="article")

        ## TAGS
        ## --------------------------------------------------------------------
        for tag in tags:
            self.insert_tag (tag, item_id=article_version_id, item_type="article")

        ## FUNDING
        ## --------------------------------------------------------------------
        for fund in funding_list:
            self.insert_funding (
                title           = conv.value_or_none (fund, "title"),
                grant_code      = conv.value_or_none (fund, "grant_code"),
                funder_name     = conv.value_or_none (fund, "funder_name"),
                is_user_defined = conv.value_or_none (fund, "is_user_defined"),
                url             = conv.value_or_none (fund, "url"),
                item_id         = article_version_id,
                item_type       = "article")

        ## CATEGORIES
        ## --------------------------------------------------------------------
        for category_id in categories:
            self.insert_article_category (article_version_id, category_id)

        ## EMBARGOS
        ## --------------------------------------------------------------------
        for embargo in embargo_options:
            self.insert_embargo (
                embargo_id         = conv.value_or_none (embargo, "id"),
                article_version_id = article_version_id,
                embargo_type       = conv.value_or_none (embargo, "type"),
                ip_name            = conv.value_or_none (embargo, "ip_name"))

        ## AUTHORS
        ## --------------------------------------------------------------------
        for author in authors:
            author_id = self.insert_author (
                author_id      = conv.value_or_none (author, "id"),
                is_active      = conv.value_or_none (author, "is_active"),
                first_name     = conv.value_or_none (author, "first_name"),
                last_name      = conv.value_or_none (author, "last_name"),
                full_name      = conv.value_or_none (author, "full_name"),
                institution_id = conv.value_or_none (author, "institution_id"),
                job_title      = conv.value_or_none (author, "job_title"),
                is_public      = conv.value_or_none (author, "is_public"),
                url_name       = conv.value_or_none (author, "url_name"),
                orcid_id       = conv.value_or_none (author, "orcid_id"))
            self.insert_article_author (article_version_id, author_id)

        ## FILES
        ## --------------------------------------------------------------------
        for file_data in files:
            self.insert_file (
                file_id       = conv.value_or_none (file_data, "id"),
                name          = conv.value_or_none (file_data, "name"),
                size          = conv.value_or_none (file_data, "size"),
                is_link_only  = conv.value_or_none (file_data, "is_link_only"),
                download_url  = conv.value_or_none (file_data, "download_url"),
                supplied_md5  = conv.value_or_none (file_data, "supplied_md5"),
                computed_md5  = conv.value_or_none (file_data, "computed_md5"),
                viewer_type   = conv.value_or_none (file_data, "viewer_type"),
                preview_state = conv.value_or_none (file_data, "preview_state"),
                status        = conv.value_or_none (file_data, "status"),
                upload_url    = conv.value_or_none (file_data, "upload_url"),
                upload_token  = conv.value_or_none (file_data, "upload_token"),
                article_version_id = article_version_id,
                account_id    = account_id)

        ## CUSTOM FIELDS
        ## --------------------------------------------------------------------
        for field in custom_fields:
            self.insert_custom_field (
                name          = conv.value_or_none (field, "name"),
                value         = conv.value_or_none (field, "value"),
                default_value = conv.value_or_none (field, "default_value"),
                max_length    = conv.value_or_none (field, "max_length"),
                min_length    = conv.value_or_none (field, "min_length"),
                field_type    = conv.value_or_none (field, "field_type"),
                is_mandatory  = conv.value_or_none (field, "is_mandatory"),
                placeholder   = conv.value_or_none (field, "placeholder"),
                is_multiple   = conv.value_or_none (field, "is_multiple"),
                item_id       = article_id,
                item_type     = "article")

        ## PRIVATE LINKS
        ## --------------------------------------------------------------------
        for link in private_links:
            self.insert_private_link (
                item_id          = article_version_id,
                item_type        = "article",
                private_link_id  = conv.value_or_none (link, "id"),
                is_active        = conv.value_or_none (link, "is_active"),
                expires_date     = conv.value_or_none (link, "expires_date"))

        ## TOPLEVEL FIELDS
        ## --------------------------------------------------------------------

        graph.add ((article_uri, RDF.type,         rdf.SG["Article"]))
        graph.add ((article_uri, rdf.COL["article_version_id"], Literal(article_version_id)))
        graph.add ((article_uri, rdf.COL["article_id"], Literal(article_id)))
        graph.add ((article_uri, rdf.COL["title"], Literal(title, datatype=XSD.string)))

        rdf.add (graph, article_uri, rdf.COL["account_id"],     account_id)
        rdf.add (graph, article_uri, rdf.COL["description"],    description,    XSD.string)
        rdf.add (graph, article_uri, rdf.COL["defined_type"],   defined_type,   XSD.string)
        rdf.add (graph, article_uri, rdf.COL["funding"],        funding,        XSD.string)
        rdf.add (graph, article_uri, rdf.COL["license_id"],     license_id)
        rdf.add (graph, article_uri, rdf.COL["doi"],            doi,            XSD.string)
        rdf.add (graph, article_uri, rdf.COL["handle"],         handle,         XSD.string)
        rdf.add (graph, article_uri, rdf.COL["resource_doi"],   resource_doi,   XSD.string)
        rdf.add (graph, article_uri, rdf.COL["resource_title"], resource_title, XSD.string)
        rdf.add (graph, article_uri, rdf.COL["group_id"],       group_id)

        current_time = datetime.strftime (datetime.now(), "%Y-%m-%d %H:%M:%S")
        rdf.add (graph, article_uri, rdf.COL["created_date"],   current_time, XSD.string)
        rdf.add (graph, article_uri, rdf.COL["modified_date"],  current_time, XSD.string)
        rdf.add (graph, article_uri, rdf.COL["published_date"], "NULL", XSD.string)
        rdf.add (graph, article_uri, rdf.COL["is_public"],      0)
        rdf.add (graph, article_uri, rdf.COL["is_active"],      1)
        rdf.add (graph, article_uri, rdf.COL["is_latest"],      0)
        rdf.add (graph, article_uri, rdf.COL["is_editable"],    1)

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            logging.info ("Inserted article %d", article_id)
            self.cache.invalidate_by_prefix ("article")
            return article_id

        return None

    def insert_account (self, account_id=None, active=None, email=None,
                        first_name=None, last_name=None, institution_user_id=None,
                        institution_id=None, pending_quota_request=None,
                        used_quota_public=None, used_quota_private=None,
                        used_quota=None, maximum_file_size=None, quota=None,
                        modified_date=None, created_date=None, group_id=None):
        """Procedure to add an account to the state graph."""

        graph = Graph()

        if account_id is None:
            account_id = self.ids.next_id("account")

        account_uri = rdf.ROW[f"account_{account_id}"]

        graph.add ((account_uri, RDF.type,      rdf.SG["Account"]))
        graph.add ((account_uri, rdf.COL["id"], Literal(account_id)))

        rdf.add (graph, account_uri, rdf.COL["active"],                active)
        rdf.add (graph, account_uri, rdf.COL["email"],                 email,         XSD.string)
        rdf.add (graph, account_uri, rdf.COL["first_name"],            first_name,    XSD.string)
        rdf.add (graph, account_uri, rdf.COL["last_name"],             last_name,     XSD.string)
        rdf.add (graph, account_uri, rdf.COL["institution_user_id"],   institution_user_id)
        rdf.add (graph, account_uri, rdf.COL["institution_id"],        institution_id)
        rdf.add (graph, account_uri, rdf.COL["group_id"],              group_id)
        rdf.add (graph, account_uri, rdf.COL["pending_quota_request"], pending_quota_request)
        rdf.add (graph, account_uri, rdf.COL["used_quota_public"],     used_quota_public)
        rdf.add (graph, account_uri, rdf.COL["used_quota_private"],    used_quota_private)
        rdf.add (graph, account_uri, rdf.COL["used_quota"],            used_quota)
        rdf.add (graph, account_uri, rdf.COL["maximum_file_size"],     maximum_file_size)
        rdf.add (graph, account_uri, rdf.COL["quota"],                 quota)
        rdf.add (graph, account_uri, rdf.COL["modified_date"],         modified_date, XSD.string)
        rdf.add (graph, account_uri, rdf.COL["created_date"],          created_date,  XSD.string)

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return account_id

        return None

    def update_account (self, account_id, active=None, email=None, job_title=None,
                        first_name=None, last_name=None, institution_user_id=None,
                        institution_id=None, pending_quota_request=None,
                        used_quota_public=None, used_quota_private=None,
                        used_quota=None, maximum_file_size=None, quota=None,
                        modified_date=None, created_date=None, group_id=None,
                        location=None, biography=None, categories=None):
        """Procedure to update account settings."""

        if modified_date is None:
            modified_date = datetime.strftime (datetime.now(), "%Y-%m-%d %H:%M:%S")

        query        = self.__query_from_template ("update_account", {
            "account_id":            account_id,
            "is_active":             active,
            "job_title":             job_title,
            "email":                 email,
            "first_name":            first_name,
            "last_name":             last_name,
            "location":              location,
            "biography":             biography,
            "institution_user_id":   institution_user_id,
            "institution_id":        institution_id,
            "pending_quota_request": pending_quota_request,
            "used_quota_public":     used_quota_public,
            "used_quota_private":    used_quota_private,
            "used_quota":            used_quota,
            "maximum_file_size":     maximum_file_size,
            "quota":                 quota,
            "modified_date":         modified_date,
            "created_date":          created_date,
            "group_id":              group_id
        })

        results = self.__run_query (query)
        if results and categories:
            self.cache.invalidate_by_prefix ("accounts")
            self.delete_account_categories (account_id)
            for category in categories:
                self.insert_account_category (account_id, category)

        return results

    def insert_institution (self, name, institution_id=None):
        """Procedure to add an institution to the state graph."""

        graph = Graph()

        if institution_id is None:
            institution_id = self.ids.next_id("institution")

        institution_uri = rdf.ROW[f"institution_{institution_id}"]

        graph.add ((institution_uri, RDF.type,      rdf.SG["Institution"]))
        graph.add ((institution_uri, rdf.COL["id"], Literal(institution_id)))

        rdf.add (graph, institution_uri, rdf.COL["name"], name, XSD.string)

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return institution_id

        return None

    def insert_author (self, author_id=None, is_active=None, first_name=None,
                       last_name=None, full_name=None, institution_id=None,
                       job_title=None, is_public=None, url_name=None,
                       orcid_id=None, email=None):
        """Procedure to add an author to the state graph."""

        graph = Graph()

        if author_id is None:
            author_id = self.ids.next_id("author")

        author_uri = rdf.ROW[f"author_{author_id}"]

        graph.add ((author_uri, RDF.type,      rdf.SG["Author"]))
        graph.add ((author_uri, rdf.COL["id"], Literal(author_id)))

        rdf.add (graph, author_uri, rdf.COL["institution_id"], institution_id)
        rdf.add (graph, author_uri, rdf.COL["is_active"],      is_active)
        rdf.add (graph, author_uri, rdf.COL["is_public"],      is_public)
        rdf.add (graph, author_uri, rdf.COL["first_name"],     first_name,     XSD.string)
        rdf.add (graph, author_uri, rdf.COL["last_name"],      last_name,      XSD.string)
        rdf.add (graph, author_uri, rdf.COL["full_name"],      full_name,      XSD.string)
        rdf.add (graph, author_uri, rdf.COL["job_title"],      job_title,      XSD.string)
        rdf.add (graph, author_uri, rdf.COL["url_name"],       url_name,       XSD.string)
        rdf.add (graph, author_uri, rdf.COL["orcid_id"],       orcid_id,       XSD.string)
        rdf.add (graph, author_uri, rdf.COL["email"],          email,          XSD.string)

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return author_id

        return None

    def delete_authors_for_item (self, item_id, account_id, author_id=None, item_type="article"):
        """Procedure to delete all authors related to an article or collection."""

        query = self.__query_from_template ("delete_authors_for_item", {
            "item_id":     item_id,
            "item_type":   item_type,
            "prefix":      item_type.capitalize(),
            "account_id":  account_id,
            "author_id":   author_id
        })

        return self.__run_query(query)

    def delete_authors_for_article (self, article_version_id, account_id, author_id=None):
        """Procedure to delete all authors related to an article."""
        return self.delete_authors_for_item (article_version_id, account_id, author_id, "article")

    def delete_authors_for_collection (self, collection_version_id, account_id, author_id=None):
        """Procedure to delete all authors related to a collection."""
        return self.delete_authors_for_item (collection_version_id, account_id, author_id, "collection")

    def delete_article_for_collection (self, collection_version_id, account_id, article_version_id=None):
        """Procedure to delete articles associated with a collection."""

        query = self.__query_from_template ("delete_article_for_collection", {
            "collection_version_id": collection_version_id,
            "account_id":    account_id,
            "article_version_id": article_version_id
        })

        self.cache.invalidate_by_prefix ("article")
        self.cache.invalidate_by_prefix (f"{collection_version_id}")

        return self.__run_query(query)

    def insert_timeline (self, revision=None, first_online=None,
                         publisher_publication=None, publisher_acceptance=None,
                         posted=None, submission=None):
        """Procedure to add a timeline to the state graph."""

        graph        = Graph()
        timeline_id  = self.ids.next_id("timeline")
        timeline_uri = rdf.ROW[f"timeline_{timeline_id}"]

        graph.add ((timeline_uri, RDF.type,      rdf.SG["Timeline"]))
        graph.add ((timeline_uri, rdf.COL["id"], Literal(timeline_id)))

        rdf.add (graph, timeline_uri, rdf.COL["revision"],             revision,              XSD.string)
        rdf.add (graph, timeline_uri, rdf.COL["firstOnline"],          first_online,          XSD.string)
        rdf.add (graph, timeline_uri, rdf.COL["publisherPublication"], publisher_publication, XSD.string)
        rdf.add (graph, timeline_uri, rdf.COL["publisherAcceptance"],  publisher_acceptance,  XSD.string)
        rdf.add (graph, timeline_uri, rdf.COL["posted"],               posted,                XSD.string)
        rdf.add (graph, timeline_uri, rdf.COL["submission"],           submission,            XSD.string)

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return timeline_id

        return None

    def insert_category (self, category_id=None, title=None, parent_id=None,
                         source_id=None, taxonomy=None):
        """Procedure to add an category to the state graph."""

        graph = Graph()

        if category_id is None:
            category_id = self.ids.next_id("category")

        category_uri = rdf.ROW[f"category_{category_id}"]

        graph.add ((category_uri, RDF.type,      rdf.SG["Category"]))
        graph.add ((category_uri, rdf.COL["id"], Literal(category_id)))

        rdf.add (graph, category_uri, rdf.COL["title"], title,         XSD.string)
        rdf.add (graph, category_uri, rdf.COL["parent_id"], parent_id)
        rdf.add (graph, category_uri, rdf.COL["source_id"], source_id)
        rdf.add (graph, category_uri, rdf.COL["taxonomy"], taxonomy)

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return category_id

        return None

    def insert_item_category (self, item_id, category_id, item_type="article"):
        """Procedure to add a link between an article or collection and a category."""

        prefix   = item_type.capitalize()
        graph    = Graph()
        link_id  = self.ids.next_id(f"{item_type}_category")
        link_uri = rdf.ROW[f"{item_type}_category_link_{link_id}"]

        graph.add ((link_uri, RDF.type,                   rdf.SG[f"{prefix}Category"]))
        graph.add ((link_uri, rdf.COL["id"],              Literal(link_id, datatype=XSD.integer)))
        graph.add ((link_uri, rdf.COL["category_id"],     Literal(category_id, datatype=XSD.integer)))
        graph.add ((link_uri, rdf.COL[f"{item_type}_version_id"], Literal(item_id, datatype=XSD.integer)))

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return link_id

        return None

    def insert_article_category (self, article_id, category_id):
        """Procedure to add a link between an article and a category."""
        return self.insert_item_category (article_id, category_id, "article")

    def insert_collection_category (self, collection_version_id, category_id):
        """Procedure to add a link between a collection and a category."""
        return self.insert_item_category (collection_version_id, category_id, "collection")

    def insert_account_category (self, account_id, category_id):
        """Procedure to add a link between an account and a category."""

        graph    = Graph()
        link_id  = self.ids.next_id("account_category")
        link_uri = rdf.ROW[f"account_category_link_{link_id}"]

        graph.add ((link_uri, RDF.type,               rdf.SG["AccountCategory"]))
        graph.add ((link_uri, rdf.COL["id"],          Literal(link_id, datatype=XSD.integer)))
        graph.add ((link_uri, rdf.COL["category_id"], Literal(category_id, datatype=XSD.integer)))
        graph.add ((link_uri, rdf.COL["account_id"],  Literal(account_id, datatype=XSD.integer)))

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return link_id

        return None

    def insert_author_link (self, author_id, item_id, item_type="article"):
        """Procedure to add a link to an author."""

        graph = Graph()

        prefix = item_type.capitalize()
        link_id  = self.ids.next_id(f"{item_type}_author")
        link_uri = rdf.ROW[f"{item_type}_author_link_{link_id}"]

        graph.add ((link_uri, RDF.type,                   rdf.SG[f"{prefix}Author"]))
        graph.add ((link_uri, rdf.COL["id"],              Literal(link_id)))
        graph.add ((link_uri, rdf.COL["author_id"],       Literal(author_id)))
        graph.add ((link_uri, rdf.COL[f"{item_type}_version_id"], Literal(item_id)))

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return link_id

        return None

    def insert_article_author (self, article_id, author_id):
        """Procedure to add a link between an article and an author."""
        return self.insert_author_link (author_id, article_id, item_type="article")

    def insert_collection_author (self, collection_version_id, author_id):
        """Procedure to add a link between a collection and a author."""
        return self.insert_author_link (author_id, collection_version_id, item_type="collection")

    def insert_article_file (self, article_version_id, file_id):
        """Procedure to add a link between an article and a file."""

        graph = Graph()

        link_id  = self.ids.next_id("article_file")
        link_uri = rdf.ROW[f"article_file_link_{link_id}"]

        graph.add ((link_uri, RDF.type,              rdf.SG["ArticleFile"]))
        graph.add ((link_uri, rdf.COL["id"],         Literal(link_id)))
        graph.add ((link_uri, rdf.COL["file_id"],    Literal(file_id, datatype=XSD.integer)))
        graph.add ((link_uri, rdf.COL["article_version_id"], Literal(article_version_id, datatype=XSD.integer)))

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return link_id

        return None

    def delete_item_categories (self, item_id, account_id, category_id=None,
                                item_type="article"):
        """Procedure to delete the categories of an article or collection."""

        prefix = item_type.capitalize()
        query = self.__query_from_template ("delete_item_categories", {
            "item_id":     item_id,
            "item_type":   item_type,
            "prefix":      prefix,
            "account_id":  account_id,
            "category_id": category_id
        })

        return self.__run_query(query)

    def delete_article_categories (self, article_id, account_id, category_id=None):
        """Procedure to delete the categories related to an article."""
        return self.delete_item_categories (article_id, account_id, category_id, "article")

    def delete_collection_categories (self, collection_version_id, account_id, category_id=None):
        """Procedure to delete the categories related to a collection."""
        return self.delete_item_categories (collection_version_id, account_id, category_id, "collection")

    def delete_account_categories (self, account_id, category_id=None):
        """Procedure to delete the categories related to an account."""

        query = self.__query_from_template ("delete_account_categories", {
            "account_id":  account_id,
            "category_id": category_id
        })

        return self.__run_query (query)

    def delete_collection_articles (self, collection_version_id, account_id):
        """Procedure to disassociate articles with a collection."""
        query = self.__query_from_template ("delete_collection_articles", {
            "collection_version_id": collection_version_id,
            "account_id":    account_id
        })

        return self.__run_query(query)

    def delete_file_for_article (self, article_version_id, account_id, file_id=None):
        """Procedure to delete a file related to an article."""

        query = self.__query_from_template ("delete_files_for_article", {
            "article_version_id": article_version_id,
            "account_id":  account_id,
            "file_id":     file_id
        })

        self.cache.invalidate_by_prefix (f"{article_version_id}_article")
        return self.__run_query(query)

    def insert_tag (self, tag, item_id=None, item_type=None):
        """Procedure to add an tag to the state graph."""

        prefix  = item_type.capitalize()
        graph   = Graph()
        tag_id  = self.ids.next_id("{item_type}_tag")
        tag_uri = rdf.ROW[f"{item_type}_tag_{tag_id}"]

        graph.add ((tag_uri, RDF.type,                   rdf.SG[f"{prefix}Tag"]))
        graph.add ((tag_uri, rdf.COL["id"],              Literal(tag_id)))
        graph.add ((tag_uri, rdf.COL[f"{item_type}_version_id"], Literal(item_id)))

        rdf.add (graph, tag_uri, rdf.COL["tag"], tag, XSD.string)

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return tag_id

        return None

    def insert_reference (self, url, item_id=None, item_type=None):
        """Procedure to add an reference to the state graph."""

        prefix        = item_type.capitalize()
        graph         = Graph()
        reference_id  = self.ids.next_id("reference")
        reference_uri = rdf.ROW[f"reference_{reference_id}"]

        graph.add ((reference_uri, RDF.type,                   rdf.SG[f"{prefix}Reference"]))
        graph.add ((reference_uri, rdf.COL["id"],              Literal(reference_id)))
        graph.add ((reference_uri, rdf.COL[f"{item_type}_version_id"], Literal(item_id)))
        graph.add ((reference_uri, rdf.COL["url"],             Literal(url, datatype=XSD.string)))

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return reference_id

        return None

    def insert_funding (self, title=None, grant_code=None, funder_name=None,
                        is_user_defined=None, url=None, item_id=None,
                        item_type=None, funding_id=None):
        """Procedure to add an funding to the state graph."""

        prefix      = item_type.capitalize()
        graph       = Graph()

        if funding_id is None:
            funding_id  = self.ids.next_id("funding")

        funding_uri = rdf.ROW[f"funding_{funding_id}"]

        graph.add ((funding_uri, RDF.type,                   rdf.SG[f"{prefix}Funding"]))
        graph.add ((funding_uri, rdf.COL["id"],              Literal(funding_id)))
        graph.add ((funding_uri, rdf.COL[f"{item_type}_version_id"], Literal(item_id)))

        rdf.add (graph, funding_uri, rdf.COL["title"],           title,           XSD.string)
        rdf.add (graph, funding_uri, rdf.COL["grant_code"],      grant_code,      XSD.string)
        rdf.add (graph, funding_uri, rdf.COL["funder_name"],     funder_name,     XSD.string)
        rdf.add (graph, funding_uri, rdf.COL["is_user_defined"], is_user_defined)
        rdf.add (graph, funding_uri, rdf.COL["url"],             url,             XSD.string)

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return funding_id

        return None

    def insert_file (self, file_id=None, name=None, size=None,
                     is_link_only=None, download_url=None, supplied_md5=None,
                     computed_md5=None, viewer_type=None, preview_state=None,
                     status=None, upload_url=None, upload_token=None,
                     article_version_id=None, account_id=None):
        """Procedure to add an file to the state graph."""

        graph    = Graph()

        if file_id is None:
            file_id  = self.ids.next_id("file")

        file_uri = rdf.ROW[f"file_{file_id}"]

        graph.add ((file_uri, RDF.type,               rdf.SG["File"]))
        graph.add ((file_uri, rdf.COL["id"],          Literal(file_id)))

        rdf.add (graph, file_uri, rdf.COL["name"],          name,          XSD.string)
        rdf.add (graph, file_uri, rdf.COL["size"],          size)
        rdf.add (graph, file_uri, rdf.COL["is_link_only"],  is_link_only)
        rdf.add (graph, file_uri, rdf.COL["download_url"],  download_url,  XSD.string)
        rdf.add (graph, file_uri, rdf.COL["supplied_md5"],  supplied_md5,  XSD.string)
        rdf.add (graph, file_uri, rdf.COL["computed_md5"],  computed_md5,  XSD.string)
        rdf.add (graph, file_uri, rdf.COL["viewer_type"],   viewer_type,   XSD.string)
        rdf.add (graph, file_uri, rdf.COL["preview_state"], preview_state, XSD.string)
        rdf.add (graph, file_uri, rdf.COL["status"],        status,        XSD.string)
        rdf.add (graph, file_uri, rdf.COL["upload_url"],    upload_url,    XSD.string)
        rdf.add (graph, file_uri, rdf.COL["upload_token"],  upload_token,  XSD.string)

        self.cache.invalidate_by_prefix ("article")
        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            if article_version_id is not None:
                if is_link_only:
                    self.update_article (article_version_id = article_version_id,
                                         account_id = account_id,
                                         has_linked_file = True)

                link_id = self.insert_article_file (article_version_id, file_id)
                if link_id is not None:
                    return file_id
            else:
                return file_id

        return None

    def update_file (self, account_id, file_id, download_url=None,
                     computed_md5=None, viewer_type=None, preview_state=None,
                     file_size=None, status=None):
        """Procedure to update file metadata."""

        query   = self.__query_from_template ("update_file", {
            "account_id":    account_id,
            "file_id":       file_id,
            "download_url":  download_url,
            "computed_md5":  computed_md5,
            "viewer_type":   viewer_type,
            "preview_state": preview_state,
            "file_size":     file_size,
            "status":        status
        })

        return self.__run_query(query)

    def insert_license (self, license_id, name=None, url=None):
        """Procedure to add an license to the state graph."""

        graph    = Graph()
        license_uri = rdf.ROW[f"license_{license_id}"]

        graph.add ((license_uri, RDF.type,               rdf.SG["License"]))
        graph.add ((license_uri, rdf.COL["id"],          Literal(license_id)))

        rdf.add (graph, license_uri, rdf.COL["name"],  name, XSD.string)
        rdf.add (graph, license_uri, rdf.COL["url"],   url,  XSD.string)

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return license_id

        return None

    def insert_private_link (self, private_link_id=None, read_only=True,
                             id_string=None, is_active=True, expires_date=None,
                             item_id=None, item_type="article"):
        """Procedure to add a private link to the state graph."""

        if id_string is None:
            id_string = secrets.token_urlsafe()

        if private_link_id is None:
            private_link_id = self.ids.next_id("private_links")

        prefix   = item_type.capitalize()
        graph    = Graph()
        link_uri = rdf.ROW[f"private_link_{id_string}"]

        graph.add ((link_uri, RDF.type,      rdf.SG[f"{prefix}PrivateLink"]))
        graph.add ((link_uri, rdf.COL["id"], Literal(private_link_id)))

        rdf.add (graph, link_uri, rdf.COL["id_string"],       id_string,    XSD.string)
        rdf.add (graph, link_uri, rdf.COL["read_only"],       read_only)
        rdf.add (graph, link_uri, rdf.COL["is_active"],       is_active)
        rdf.add (graph, link_uri, rdf.COL["expires_date"],    expires_date, XSD.string)
        rdf.add (graph, link_uri, rdf.COL[f"{item_type}_version_id"], item_id)

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return id_string

        return None

    def insert_embargo (self, embargo_id, article_version_id, embargo_type=None, ip_name=None):
        """Procedure to add an license to the state graph."""

        graph    = Graph()
        embargo_uri = rdf.ROW[f"embargo_{embargo_id}"]

        graph.add ((embargo_uri, RDF.type,               rdf.SG["ArticleEmbargoOption"]))
        graph.add ((embargo_uri, rdf.COL["id"],          Literal(embargo_id)))
        graph.add ((embargo_uri, rdf.COL["article_version_id"], Literal(article_version_id)))

        rdf.add (graph, embargo_uri, rdf.COL["type"],    embargo_type, XSD.string)
        rdf.add (graph, embargo_uri, rdf.COL["ip_name"], ip_name,      XSD.string)

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return embargo_id

        return None

    def insert_custom_field (self, name=None, value=None, default_value=None,
                             max_length=None, min_length=None, field_type=None,
                             is_mandatory=None, placeholder=None,
                             is_multiple=None, item_id=None,
                             item_type="article"):
        """Procedure to add a custom field to the state graph."""

        prefix           = item_type.capitalize()
        graph            = Graph()
        custom_field_id  = self.ids.next_id("custom_field")
        custom_field_uri = rdf.ROW[f"custom_field_{custom_field_id}"]

        graph.add ((custom_field_uri, RDF.type,                   rdf.SG[f"{prefix}CustomField"]))
        graph.add ((custom_field_uri, rdf.COL["id"],              Literal(custom_field_id)))
        graph.add ((custom_field_uri, rdf.COL[f"{item_type}_version_id"], Literal(item_id)))

        rdf.add (graph, custom_field_uri, rdf.COL["name"],          name,          XSD.string)
        rdf.add (graph, custom_field_uri, rdf.COL["value"],         value)
        rdf.add (graph, custom_field_uri, rdf.COL["default_value"], default_value)
        rdf.add (graph, custom_field_uri, rdf.COL["max_length"],    max_length)
        rdf.add (graph, custom_field_uri, rdf.COL["min_length"],    min_length)
        rdf.add (graph, custom_field_uri, rdf.COL["field_type"],    field_type)
        rdf.add (graph, custom_field_uri, rdf.COL["is_mandatory"],  is_mandatory)
        rdf.add (graph, custom_field_uri, rdf.COL["placeholder"],   placeholder)
        rdf.add (graph, custom_field_uri, rdf.COL["is_multiple"],   is_multiple)

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return custom_field_id

        return None

    def delete_article (self, article_id, account_id):
        """Procedure to remove an article from the state graph."""

        query   = self.__query_from_template ("delete_article", {
            "account_id":  account_id,
            "article_id":  article_id
        })

        result = self.__run_query(query)
        self.cache.invalidate_by_prefix (f"{article_id}_article")
        self.cache.invalidate_by_prefix ("article")

        return result

    def delete_article_version (self, article_version_id, account_id):
        """Procedure to remove an article from the state graph."""

        query   = self.__query_from_template ("delete_article_version", {
            "account_id":          account_id,
            "article_version_id":  article_version_id
        })

        result = self.__run_query (query)
        self.cache.invalidate_by_prefix (f"{article_version_id}_article")
        self.cache.invalidate_by_prefix ("article")

        return result

    def update_article (self, article_version_id, account_id, title=None,
                        description=None, resource_doi=None,
                        resource_title=None, license_id=None, group_id=None,
                        time_coverage=None, publisher=None, language=None,
                        mimetype=None, contributors=None, license_remarks=None,
                        geolocation=None, longitude=None, latitude=None,
                        data_link=None, has_linked_file=None, derived_from=None,
                        same_as=None, organizations=None, categories=None,
                        defined_type=None, defined_type_name=None):
        """Procedure to overwrite parts of an article."""

        query   = self.__query_from_template ("update_article", {
            "account_id":      account_id,
            "article_version_id": article_version_id,
            "contributors":    contributors,
            "data_link":       data_link,
            "defined_type":    defined_type,
            "defined_type_name": defined_type_name,
            "derived_from":    derived_from,
            "description":     description,
            "format":          mimetype,
            "geolocation":     geolocation,
            "has_linked_file": has_linked_file,
            "language":        language,
            "latitude":        latitude,
            "license_id":      license_id,
            "group_id":        group_id,
            "license_remarks": license_remarks,
            "longitude":       longitude,
            "modified_date":   datetime.strftime (datetime.now(), "%Y-%m-%d %H:%M:%S"),
            "organizations":   organizations,
            "publisher":       publisher,
            "resource_doi":    resource_doi,
            "resource_title":  resource_title,
            "same_as":         same_as,
            "time_coverage":   time_coverage,
            "title":           title
        })

        self.cache.invalidate_by_prefix ("article")
        self.cache.invalidate_by_prefix (f"{article_version_id}_article")
        results = self.__run_query (query, query, f"{article_version_id}_article")
        if results and categories:
            self.delete_article_categories (article_version_id, account_id)
            for category in categories:
                self.insert_article_category (article_version_id, category)

        return results

    def delete_article_embargo (self, article_version_id, account_id):
        """Procedure to lift the embargo on an article."""

        query   = self.__query_from_template ("delete_article_embargo", {
            "account_id":  account_id,
            "article_version_id":  article_version_id
        })

        return self.__run_query(query)

    def delete_private_links (self, item_id, account_id, link_id, item_type="article"):
        """Procedure to remove private links to an article."""

        prefix  = item_type.capitalize()
        query   = self.__query_from_template ("delete_private_links", {
            "account_id":  account_id,
            "item_id":     item_id,
            "item_type":   item_type,
            "prefix":      prefix,
            "id_string":   link_id
        })

        return self.__run_query(query)

    def update_private_link (self, item_id, account_id, link_id,
                             is_active=None, expires_date=None,
                             read_only=None, item_type="article"):
        """Procedure to update a private link to an article."""

        prefix  = item_type.capitalize()
        query   = self.__query_from_template ("update_private_link", {
            "account_id":   account_id,
            "item_id":      item_id,
            "item_type":    item_type,
            "prefix":       prefix,
            "id_string":    link_id,
            "is_active":    is_active,
            "expires_date": expires_date,
            "read_only":    read_only
        })

        return self.__run_query(query)

    def article_update_thumb (self, article_id, version, account_id, file_id):
        """Procedure to update the thumbnail of an article."""

        filters = rdf.sparql_filter ("file_id", file_id)
        query   = self.__query_from_template ("update_article_thumb", {
            "account_id":  account_id,
            "article_id":  article_id,
            "version":     version,
            "filters":     filters
        })

        return self.__run_query(query)

    def insert_collection_article (self, collection_version_id, article_version_id):
        """Procedure to add an article to a collection."""

        if collection_version_id is None or article_version_id is None:
            return False

        graph       = Graph()
        link_id  = self.ids.next_id("collection_article")
        link_uri = rdf.ROW[f"collection_article_{link_id}"]

        graph.add ((link_uri, RDF.type,                  rdf.SG["CollectionArticle"]))
        graph.add ((link_uri, rdf.COL["id"],             Literal(link_id)))
        graph.add ((link_uri, rdf.COL["collection_version_id"],  Literal(collection_version_id)))
        graph.add ((link_uri, rdf.COL["article_version_id"],     Literal(article_version_id)))

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            self.cache.invalidate_by_prefix ("article")
            return link_id

        return None

    def insert_collection (self, title,
                           account_id,
                           collection_id=None,
                           collection_version_id=None,
                           funding=None,
                           funding_list=None,
                           description=None,
                           articles=None,
                           authors=None,
                           categories=None,
                           categories_by_source_id=None,
                           tags=None,
                           keywords=None,
                           references=None,
                           custom_fields=None,
                           custom_fields_list=None,
                           doi=None,
                           handle=None,
                           url=None,
                           resource_id=None,
                           resource_doi=None,
                           resource_link=None,
                           resource_title=None,
                           resource_version=None,
                           group_id=None,
                           first_online=None,
                           publisher_publication=None,
                           publisher_acceptance=None,
                           submission=None,
                           posted=None,
                           revision=None,
                           private_links=None):
        """Procedure to insert a collection to the state graph."""

        funding_list            = [] if funding_list            is None else funding_list
        tags                    = [] if tags                    is None else tags
        references              = [] if references              is None else references
        categories              = [] if categories              is None else categories
        categories_by_source_id = [] if categories_by_source_id is None else categories_by_source_id
        authors                 = [] if authors                 is None else authors
        custom_fields           = [] if custom_fields           is None else custom_fields
        custom_fields_list      = [] if custom_fields_list      is None else custom_fields_list
        private_links           = [] if private_links           is None else private_links
        articles                = [] if articles                is None else articles

        graph = Graph()

        if collection_version_id is None:
            collection_version_id = self.ids.next_id("article")

        if collection_id is None:
            collection_id = collection_version_id

        collection_uri = rdf.ROW[f"collection_{collection_version_id}"]

        ## TIMELINE
        ## --------------------------------------------------------------------
        timeline_id = self.insert_timeline (
            revision              = revision,
            first_online          = first_online,
            publisher_publication = publisher_publication,
            publisher_acceptance  = publisher_acceptance,
            posted                = posted,
            submission            = submission
        )

        rdf.add (graph, collection_uri, rdf.COL["timeline_id"], timeline_id)

        ## REFERENCES
        ## --------------------------------------------------------------------
        for reference in references:
            self.insert_reference (reference, item_id=collection_version_id, item_type="collection")

        ## TAGS
        ## --------------------------------------------------------------------
        for tag in tags:
            self.insert_tag (tag, item_id=collection_version_id, item_type="collection")

        ## FUNDING
        ## --------------------------------------------------------------------
        for fund in funding_list:
            self.insert_funding (
                funding_id      = conv.value_or_none (fund, "id"),
                title           = conv.value_or_none (fund, "title"),
                grant_code      = conv.value_or_none (fund, "grant_code"),
                funder_name     = conv.value_or_none (fund, "funder_name"),
                is_user_defined = conv.value_or_none (fund, "is_user_defined"),
                url             = conv.value_or_none (fund, "url"),
                item_id         = collection_version_id,
                item_type       = "collection")

        ## CATEGORIES
        ## --------------------------------------------------------------------
        for category in categories:
            category_id = self.insert_category (
                category_id = conv.value_or_none (category, "id"),
                title       = conv.value_or_none (category, "title"),
                parent_id   = conv.value_or_none (category, "parent_id"),
                source_id   = conv.value_or_none (category, "source_id"),
                taxonomy    = conv.value_or_none (category, "taxonomy"))
            self.insert_collection_category (collection_version_id, category_id)

        ## ARTICLES
        ## --------------------------------------------------------------------
        for article_id in articles:
            self.insert_collection_article (collection_version_id, article_id)

        ## AUTHORS
        ## --------------------------------------------------------------------
        for author in authors:
            author_id = self.insert_author (
                author_id      = conv.value_or_none (author, "id"),
                is_active      = conv.value_or_none (author, "is_active"),
                first_name     = conv.value_or_none (author, "first_name"),
                last_name      = conv.value_or_none (author, "last_name"),
                full_name      = conv.value_or_none (author, "full_name"),
                institution_id = conv.value_or_none (author, "institution_id"),
                job_title      = conv.value_or_none (author, "job_title"),
                is_public      = conv.value_or_none (author, "is_public"),
                url_name       = conv.value_or_none (author, "url_name"),
                orcid_id       = conv.value_or_none (author, "orcid_id"))
            self.insert_collection_author (collection_version_id, author_id)

        ## CUSTOM FIELDS
        ## --------------------------------------------------------------------
        for field in custom_fields:
            self.insert_custom_field (
                name          = conv.value_or_none (field, "name"),
                value         = conv.value_or_none (field, "value"),
                default_value = conv.value_or_none (field, "default_value"),
                max_length    = conv.value_or_none (field, "max_length"),
                min_length    = conv.value_or_none (field, "min_length"),
                field_type    = conv.value_or_none (field, "field_type"),
                is_mandatory  = conv.value_or_none (field, "is_mandatory"),
                placeholder   = conv.value_or_none (field, "placeholder"),
                is_multiple   = conv.value_or_none (field, "is_multiple"),
                item_id       = collection_version_id,
                item_type     = "collection")

        ## PRIVATE LINKS
        ## --------------------------------------------------------------------
        for link in private_links:
            self.insert_private_link (
                item_id          = collection_version_id,
                item_type        = "collection",
                private_link_id  = conv.value_or_none (link, "id"),
                is_active        = conv.value_or_none (link, "is_active"),
                expires_date     = conv.value_or_none (link, "expires_date"))

        ## TOPLEVEL FIELDS
        ## --------------------------------------------------------------------

        graph.add ((collection_uri, RDF.type,         rdf.SG["Collection"]))
        graph.add ((collection_uri, rdf.COL["collection_id"],    Literal(collection_id)))
        graph.add ((collection_uri, rdf.COL["collection_version_id"], Literal(collection_version_id)))
        graph.add ((collection_uri, rdf.COL["title"], Literal(title, datatype=XSD.string)))

        rdf.add (graph, collection_uri, rdf.COL["account_id"],     account_id)
        rdf.add (graph, collection_uri, rdf.COL["description"],    description,    XSD.string)
        rdf.add (graph, collection_uri, rdf.COL["funding"],        funding,        XSD.string)
        rdf.add (graph, collection_uri, rdf.COL["doi"],            doi,            XSD.string)
        rdf.add (graph, collection_uri, rdf.COL["handle"],         handle,         XSD.string)
        rdf.add (graph, collection_uri, rdf.COL["url"],            url,            XSD.string)
        rdf.add (graph, collection_uri, rdf.COL["resource_doi"],   resource_doi,   XSD.string)
        rdf.add (graph, collection_uri, rdf.COL["resource_title"], resource_title, XSD.string)
        rdf.add (graph, collection_uri, rdf.COL["group_id"],       group_id)

        current_time = datetime.strftime (datetime.now(), "%Y-%m-%d %H:%M:%S")
        rdf.add (graph, collection_uri, rdf.COL["created_date"],   current_time, XSD.string)
        rdf.add (graph, collection_uri, rdf.COL["modified_date"],  current_time, XSD.string)
        rdf.add (graph, collection_uri, rdf.COL["published_date"], "NULL", XSD.string)
        rdf.add (graph, collection_uri, rdf.COL["is_public"],      0)

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            logging.info ("Inserted collection %d", collection_id)
            return collection_id

        return None

    def delete_collection (self, collection_version_id, account_id):
        """Procedure to remove a collection from the state graph."""

        query   = self.__query_from_template ("delete_collection", {
            "account_id":    account_id,
            "collection_version_id": collection_version_id
        })

        return self.__run_query(query)

    def update_collection (self, collection_version_id, account_id, title=None,
                           description=None, resource_doi=None,
                           resource_title=None, group_id=None, articles=None,
                           time_coverage=None, publisher=None, language=None,
                           contributors=None, geolocation=None, longitude=None,
                           latitude=None, organizations=None, categories=None):
        """Procedure to overwrite parts of a collection."""

        query   = self.__query_from_template ("update_collection", {
            "account_id":        account_id,
            "collection_version_id": collection_version_id,
            "contributors":      contributors,
            "description":       description,
            "geolocation":       geolocation,
            "language":          language,
            "latitude":          latitude,
            "group_id":          group_id,
            "longitude":         longitude,
            "modified_date":     datetime.strftime (datetime.now(), "%Y-%m-%d %H:%M:%S"),
            "organizations":     organizations,
            "publisher":         publisher,
            "resource_doi":      resource_doi,
            "resource_title":    resource_title,
            "time_coverage":     time_coverage,
            "title":             title
        })

        self.cache.invalidate_by_prefix ("collection")
        self.cache.invalidate_by_prefix (f"{collection_version_id}_collection")

        results = self.__run_query (query, query, f"{collection_version_id}_collection")
        if results and categories:
            self.delete_collection_categories (collection_version_id, account_id)
            for category in categories:
                self.insert_collection_category (collection_version_id, category)

        if results and articles:
            self.delete_collection_articles (collection_version_id, account_id)
            for article_id in articles:
                self.insert_collection_article (collection_version_id, article_id)

        return results

    def category_by_id (self, category_id):
        """Procedure to return category information by its identifier."""

        query = self.__query_from_template ("category_by_id", {
            "category_id": category_id
        })

        try:
            results = self.__run_query (query, query, "category")
            return results[0]
        except IndexError:
            return None

    def subcategories_for_category (self, category_id):
        """Procedure to return the subcategories for a category."""

        query = self.__query_from_template ("subcategories_by_category", {
            "category_id": category_id
        })

        return self.__run_query (query, query, "category")

    def root_categories (self):
        """Procedure to return the categories without a parent category."""

        query = self.__query_from_template ("root_categories", {
            "state_graph": self.state_graph
        })

        query += rdf.sparql_suffix ("title", "asc")
        return self.__run_query (query, query, "category")

    def categories_tree (self):
        """Procedure to return a tree of categories."""

        categories = self.root_categories ()
        for index, _ in enumerate(categories):
            category      = categories[index]
            subcategories = self.subcategories_for_category (category["id"])
            categories[index]["subcategories"] = subcategories

        return categories

    def group (self, group_id=None, parent_id=None, name=None,
               association=None, limit=None, offset=None,
               order=None, order_direction=None, starts_with=False):
        """Procedure to return group information."""

        filters = ""
        if group_id is not None:
            filters += rdf.sparql_filter ("id", group_id)

        if parent_id is not None:
            filters += rdf.sparql_filter ("parent_id", parent_id)

        if name is not None:
            if starts_with:
                filters += f"FILTER (STRSTARTS(STR(?name), \"{name}\"))"
            else:
                filters += rdf.sparql_filter ("name", name, escape=True)

        if association is not None:
            filters += rdf.sparql_filter ("association", association, escape=True)

        query = self.__query_from_template ("group", {
            "filters":     filters
        })

        query += rdf.sparql_suffix (order, order_direction, limit, offset)

        return self.__run_query (query, query, "group")

    def group_by_name (self, group_name, startswith=False):
        """Procedure to return group information by its name."""

        query = self.__query_from_template ("group_by_name", {
            "startswith": startswith,
            "group_name": group_name
        })

        results = self.__run_query (query, query, "group")
        if startswith:
            return results
        try:
            return results[0]
        except IndexError:
            return None

    def account_storage_used (self, account_id):
        """Returns the number of bytes used by an account."""

        query = self.__query_from_template ("account_storage_used", {
            "account_id":  account_id
        })

        files = self.__run_query (query, query, "storage")
        try:
            number_of_bytes = 0
            for entry in files:
                number_of_bytes += int(float(entry["bytes"]))
            return number_of_bytes
        except IndexError:
            pass
        except KeyError:
            pass

        return 0

    def opendap_to_doi(self, startswith=None, endswith=None):
        """Procedure to return DOI corresponding to opendap catalog url"""

        filters = ""

        if startswith is not None:
            if isinstance(startswith, list):
                filters += f"FILTER ((STRSTARTS(STR(?download_url), \"{ startswith[0] }\"))"
                for filter_item in startswith[1:]:
                    filters += f" OR (STRSTARTS(STR(?download_url), \"{filter_item}\"))"
                filters += ")\n"
            elif isinstance(startswith, str):
                filters += f"FILTER (STRSTARTS(STR(?download_url), \"{ startswith }\"))\n"
            else:
                logging.error("startswith of type %s is not supported", type(startswith))

        if endswith is not None:
            filters += f"FILTER (STRENDS(STR(?download_url), \"{ endswith }\"))\n"

        query = self.__query_from_template ("opendap_to_doi", {
            "filters": filters
        })

        results = self.__run_query (query)
        return results

    def account_id_by_orcid (self, orcid):
        """Returns the account ID belonging to an ORCID."""

        query = self.__query_from_template ("account_id_by_orcid", {
            "orcid":       orcid
        })

        try:
            results = self.__run_query (query)
            return results[0]["account_id"]
        except IndexError:
            return None
        except KeyError:
            return None

    def account_by_session_token (self, session_token):
        """Returns an account_id or None."""

        query = self.__query_from_template ("account_by_session_token", {
            "token":       session_token
        })

        try:
            results = self.__run_query (query)
            account = results[0]
            privileges = self.privileges[int(account["account_id"])]
            account = { **account, **privileges }
            return account
        except IndexError:
            return None
        except KeyError:
            return account

    def accounts (self, account_id=None):
        """Returns accounts."""

        query = self.__query_from_template ("accounts", {
            "account_id":  account_id
        })

        return self.__run_query (query, query, "accounts")

    def account_by_id (self, account_id):
        """Returns an account_id or None."""

        query = self.__query_from_template ("account_by_id", {
            "account_id":  account_id
        })

        try:
            results    = self.__run_query (query)
            account    = results[0]
            privileges = self.privileges[int(account["account_id"])]
            account    = { **account, **privileges }
            return account
        except IndexError:
            return None
        except KeyError:
            return account

    def insert_session (self, account_id, name=None, token=None, editable=False):
        """Procedure to add a session token for an account_id."""

        if account_id is None:
            return None, None

        if token is None:
            token = secrets.token_hex (64)

        current_time = datetime.strftime (datetime.now(), "%Y-%m-%dT%H:%M:%SZ")

        graph       = Graph()
        link_id     = self.ids.next_id("session")
        link_uri    = rdf.ROW[f"session_link_{link_id}"]
        graph.add ((link_uri, RDF.type,              rdf.SG["Session"]))
        graph.add ((link_uri, rdf.COL["account_id"], Literal(account_id)))
        graph.add ((link_uri, rdf.COL["id"],         Literal(link_id)))
        graph.add ((link_uri, rdf.COL["created_date"], Literal(current_time, datatype=XSD.dateTime)))
        graph.add ((link_uri, rdf.COL["name"],       Literal(name, datatype=XSD.string)))
        graph.add ((link_uri, rdf.COL["token"],      Literal(token, datatype=XSD.string)))
        graph.add ((link_uri, rdf.COL["editable"],   Literal(editable, datatype=XSD.boolean)))

        query = self.__insert_query_for_graph (graph)
        if self.__run_query(query):
            return token, link_id

        return None, None

    def update_session (self, account_id, session_id, name):
        """Procedure to edit a session."""

        query = self.__query_from_template ("update_session", {
            "account_id":    account_id,
            "session_id":    session_id,
            "name":          name
        })

        return self.__run_query (query)

    def delete_session_by_id (self, account_id, session_id):
        """Procedure to remove a session from the state graph."""

        query   = self.__query_from_template ("delete_session_by_id", {
            "session_id":  session_id,
            "account_id":  account_id
        })

        return self.__run_query (query)

    def delete_session (self, token):
        """Procedure to remove a session from the state graph."""

        if token is None:
            return True

        query   = self.__query_from_template ("delete_session", {
            "token":       token
        })

        return self.__run_query(query)

    def sessions (self, account_id, session_id=None):
        """Returns the sessions for an account."""

        query = self.__query_from_template ("account_sessions", {
            "account_id":  account_id,
            "session_id":  session_id
        })

        return self.__run_query (query)

    def __may_execute_role (self, session_token, task):
        """Returns True when the sessions' account may perform 'task'."""
        account = self.account_by_session_token (session_token)
        try:
            return account[f"may_{task}"]
        except KeyError:
            pass
        except TypeError:
            pass

        return False

    def may_administer (self, session_token):
        """Returns True when the session's account is an administrator."""
        return self.__may_execute_role (session_token, "administer")

    def may_impersonate (self, session_token):
        """Returns True when the session's account may impersonate other accounts."""
        return self.__may_execute_role (session_token, "impersonate")

    def is_depositor (self, session_token):
        """Returns True when the account linked to the session is a depositor, False otherwise"""
        account = self.account_by_session_token (session_token)
        return account is not None

    def is_logged_in (self, session_token):
        """Returns True when the session_token is valid, False otherwise."""
        account = self.account_by_session_token (session_token)
        return account is not None
