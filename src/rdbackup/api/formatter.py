"""
This module contains procedures to format a record from
rdbackup.database to be backward-compatible with Figshare.
"""

from rdbackup.utils import convenience as conv

def format_article_record (record):
    return {
        "id":                      conv.value_or_none(record, "id"),
        "title":                   conv.value_or_none(record, "title"),
        "doi":                     conv.value_or_none(record, "doi"),
        "handle":                  conv.value_or_none(record, "handle"),
        "url":                     conv.value_or_none(record, "url"),
        "published_date":          conv.value_or_none(record, "published_date"),
        "thumb":                   conv.value_or_none(record, "thumb"),
        "defined_type":            conv.value_or_none(record, "defined_type"),
        "defined_type_name":       conv.value_or_none(record, "defined_type_name"),
        "group_id":                conv.value_or_none(record, "group_id"),
        "url_private_api":         conv.value_or_none(record, "url_private_api"),
        "url_public_api":          conv.value_or_none(record, "url_public_api"),
        "url_private_html":        conv.value_or_none(record, "url_private_html"),
        "url_public_html":         conv.value_or_none(record, "url_public_html"),
        "timeline": {
            "posted":              conv.value_or_none(record, "timeline_posted"),
            "firstOnline":         conv.value_or_none(record, "timeline_first_online"),
            "revision":            conv.value_or_none(record, "timeline_revision"),
            "publisherAcceptance": conv.value_or_none(record, "timeline_publisher_acceptance"),
        },
        "resource_title":          conv.value_or_none(record, "resource_title"),
        "resource_doi":            conv.value_or_none(record, "resource_doi")
    }

def format_author_record (record):
    return {
      "id":        conv.value_or_none(record, "id"),
      "full_name": conv.value_or_none(record, "full_name"),
      "is_active": bool(conv.value_or_none(record, "is_active")),
      "url_name":  conv.value_or_none(record, "url_name"),
      "orcid_id":  conv.value_or(record, "orcid_id", "")
    }

def format_file_for_article_record (record):
    return {
      "id":           conv.value_or_none(record, "id"),
      "name":         conv.value_or_none(record, "name"),
      "size":         conv.value_or_none(record, "size"),
      "is_link_only": bool(conv.value_or_none(record, "is_link_only")),
      "download_url": conv.value_or_none(record, "download_url"),
      "supplied_md5": conv.value_or_none(record, "supplied_md5"),
      "computed_md5": conv.value_or_none(record, "computed_md5")
    }

def format_file_details_record (record):
    return {
      "status":        conv.value_or_none(record, "status"),
      "viewer_type":   conv.value_or_none(record, "viewer_type"),
      "preview_state": conv.value_or_none(record, "preview_state"),
      "upload_url":    conv.value_or_none(record, "upload_url"),
      "upload_token":  conv.value_or_none(record, "upload_token"),
      "id":            conv.value_or_none(record, "id"),
      "name":          conv.value_or_none(record, "name"),
      "size":          conv.value_or_none(record, "size"),
      "is_link_only":  conv.value_or_none(record, "is_link_only"),
      "download_url":  conv.value_or_none(record, "download_url"),
      "supplied_md5":  conv.value_or_none(record, "supplied_md5"),
      "computed_md5":  conv.value_or_none(record, "computed_md5")
    }

def format_custom_field_record (record):
    return {
      "name":         conv.value_or_none(record, "name"),
      "value":        conv.value_or_none(record, "value"),
    }

def format_category_record (record):
    return {
        "id":          conv.value_or_none(record, "id"),
        "title":       conv.value_or_none(record, "title"),
        "parent_id":   conv.value_or_none(record, "parent_id"),
        "path":        conv.value_or(record, "path", ""),
        "source_id":   conv.value_or_none(record, "source_id"),
        "taxonomy_id": conv.value_or_none(record, "taxonomy_id"),
    }

def format_tag_record (record):
    return conv.value_or_none(record, "tag")

def format_article_details_record (article, authors, files, custom_fields,
                                   embargo_options, tags, categories, funding,
                                   references):
    return {
        "files":             list (map (format_file_for_article_record, files)),
        "custom_fields":     list (map (format_custom_field_record, custom_fields)),
        "authors":           list (map (format_author_record, authors)),
        "figshare_url":      conv.value_or_none(article, "figshare_url"),
        "description":       conv.value_or_none(article, "description"),
        "funding":           conv.value_or_none(article, "funding"),
        "funding_list":      list (map (format_funding_record, funding)),
        "version":           conv.value_or_none(article, "version"),
        "status":            conv.value_or_none(article, "status"),
        "size":              conv.value_or_none(article, "size"),
        "created_date":      conv.value_or_none(article, "created_date"),
        "modified_date":     conv.value_or_none(article, "modified_date"),
        "is_public":         bool(conv.value_or_none(article, "is_public")),
        "is_confidential":   bool(conv.value_or_none(article, "is_confidential")),
        "is_metadata_record": bool(conv.value_or_none(article, "is_metadata_record")),
        "confidential_reason": conv.value_or(article, "confidential_reason", ""),
        "metadata_reason":   conv.value_or(article, "metadata_reason", ""),
        "license": {
            "value":         conv.value_or_none(article, "license_id"),
            "name":          conv.value_or_none(article, "license_name"),
            "url":           conv.value_or_none(article, "license_url"),
        },
        "tags":              list (map (format_tag_record, tags)),
        "categories":        list (map (format_category_record, categories)),
        "references":        references,
        "has_linked_file":   bool(conv.value_or_none(article, "has_linked_file")),
        "citation":          conv.value_or_none(article, "citation"),
#        "is_active":         conv.value_or_none(article, "is_active"),
        "is_embargoed":      bool(conv.value_or_none(article, "is_embargoed")),
        "embargo_date":      conv.value_or_none(article, "embargo_date"),
        "embargo_type":      conv.value_or(article, "embargo_type", "file"),
        "embargo_title":     conv.value_or(article, "embargo_title", ""),
        "embargo_reason":    conv.value_or(article, "embargo_reason", ""),
        "embargo_options":   list (map (format_article_embargo_option_record, embargo_options)),
        "id":                conv.value_or_none(article, "id"),
        "title":             conv.value_or_none(article, "title"),
        "doi":               conv.value_or_none(article, "doi"),
        "handle":            conv.value_or(article, "handle", ""),
        "url":               conv.value_or_none(article, "url"),
        "published_date":    conv.value_or_none(article, "published_date"),
        "thumb":             conv.value_or(article, "thumb", ""),
        "defined_type":      conv.value_or_none(article, "defined_type"),
        "defined_type_name": conv.value_or_none(article, "defined_type_name"),
        "group_id":          conv.value_or_none(article, "group_id"),
        "url_private_api":   conv.value_or_none(article, "url_private_api"),
        "url_public_api":    conv.value_or_none(article, "url_public_api"),
        "url_private_html":  conv.value_or_none(article, "url_private_html"),
        "url_public_html":   conv.value_or_none(article, "url_public_html"),
        "timeline": {
            "posted":        conv.value_or_none(article, "timeline_posted"),
            "revision":      conv.value_or_none(article, "timeline_revision"),
            "submission":    conv.value_or_none(article, "timeline_submission"),
            "firstOnline":   conv.value_or_none(article, "timeline_first_online"),
            "publisherPublication": conv.value_or_none(article, "timeline_publisher_publication"),
            "publisherAcceptance": conv.value_or_none(article, "timeline_publisher_acceptance"),
        },
        "resource_title":    conv.value_or(article, "resource_title", ""),
        "resource_doi":      conv.value_or(article, "resource_doi", ""),
    }

def format_article_embargo_option_record (record):
    return {
        "id":                conv.value_or_none (record, "id"),
        "type":              conv.value_or_none (record, "type"),
        "ip_name":           conv.value_or_none (record, "ip_name")
    }

def format_article_embargo_record (article, embargo_options):
    return {
        "is_embargoed":      bool(conv.value_or_none(article, "is_embargoed")),
        "embargo_date":      conv.value_or_none(article, "embargo_date"),
        "embargo_type":      conv.value_or(article, "embargo_type", "file"),
        "embargo_title":     conv.value_or(article, "embargo_title", ""),
        "embargo_reason":    conv.value_or(article, "embargo_reason", ""),
        "embargo_options":   list (map (format_article_embargo_option_record, embargo_options)),
    }

def format_article_confidentiality_record (article):
    return {
        "is_confidential":   bool(conv.value_or_none(article, "is_confidential")),
        "reason": conv.value_or(article, "confidential_reason", ""),
    }

def format_article_version_record (record):
    return {
        "version":           conv.value_or_none(record, "version"),
        "url":               conv.value_or_none(record, "url")
    }

def format_collection_record (record):
    return {
        "id":                conv.value_or_none(record, "id"),
        "title":             conv.value_or_none(record, "title"),
        "doi":               conv.value_or_none(record, "doi"),
        "handle":            conv.value_or(record, "handle", ""),
        "url":               conv.value_or_none(record, "url"),
        "timeline": {
            "posted":        conv.value_or_none(record, "timeline_posted"),
            "submission":    conv.value_or_none(record, "timeline_submission"),
            "revision":      conv.value_or_none(record, "timeline_revision"),
            "firstOnline":   conv.value_or_none(record, "timeline_first_online"),
            "publisherPublication": conv.value_or_none(record, "timeline_publisher_publication"),
            "publisherAcceptance": conv.value_or_none(record, "timeline_publisher_acceptance"),
        },
        "published_date":    conv.value_or_none(record, "published_date"),
    }

def format_collection_details_record (collection, funding, categories,
                                      references, tags, authors, custom_fields):
    return {
        "version":           conv.value_or_none(collection, "version"),
        "resource_id":       conv.value_or(collection, "resource_id", ""),
        "resource_doi":      conv.value_or(collection, "resource_doi", ""),
        "resource_title":    conv.value_or(collection, "resource_title", ""),
        "resource_link":     conv.value_or(collection, "resource_link", ""),
        "resource_version":  conv.value_or_none(collection, "resource_version"),
        "description":       conv.value_or_none(collection, "description"),
        "categories":        list (map (format_category_record, categories)),
        "references":        references,
        "tags":              list (map (format_tag_record, tags)),
        "authors":           list (map (format_author_record, authors)),
        "funding":           list (map (format_funding_record, funding)),
        "institution_id":    conv.value_or_none(collection, "institution_id"),
        "group_id":          conv.value_or_none(collection, "group_id"),
        "group_resource_id": conv.value_or_none(collection, "group_resource_id"),
        "articles_count":    conv.value_or_none(collection, "articles_count"),
        "public":            bool(conv.value_or_none(collection, "is_public")),
        "custom_fields":     list (map (format_custom_field_record, custom_fields)),
        "citation":          conv.value_or_none(collection, "citation"),
        "created_date":      conv.value_or_none(collection, "created_date"),
        "modified_date":     conv.value_or_none(collection, "modified_date"),
        "id":                conv.value_or_none(collection, "id"),
        "title":             conv.value_or_none(collection, "title"),
        "doi":               conv.value_or_none(collection, "doi"),
        "handle":            conv.value_or(collection, "handle", ""),
        "url":               conv.value_or_none(collection, "url"),
        "published_date":    conv.value_or_none(collection, "published_date"),
        "timeline": {
            "posted":               conv.value_or_none(collection, "timeline_posted"),
            "firstOnline":          conv.value_or_none(collection, "timeline_first_online"),
            "revision":             conv.value_or_none(collection, "timeline_revision"),
            "publisherAcceptance":  conv.value_or_none(collection, "timeline_publisher_acceptance"),
            "submission":           conv.value_or_none(collection, "timeline_submission"),
            "publisherPublication": conv.value_or_none(collection, "timeline_publisher_publication")
        }
    }

def format_funding_record (record):
    return {
        "id":                conv.value_or_none(record, "id"),
        "title":             conv.value_or_none(record, "title"),
        "grant_code":        conv.value_or_none(record, "grant_code"),
        "funder_name":       conv.value_or_none(record, "funder_name"),
        "is_user_defined":   conv.value_or_none(record, "is_user_defined"),
        "url":               conv.value_or_none(record, "url")
    }

def format_version_record (record):
    version = conv.value_or_none (record, "version")
    url     = conv.value_or_none (record, "url_public_api")

    return {
        "version": version,
        "url": f"{url}/versions/{version}"
    }
