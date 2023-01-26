"""This module contains the command-line interface for the 'web' subcommand."""

import logging
import os
import shutil
import json
from defusedxml import ElementTree
from werkzeug.serving import run_simple
from djehuty.web import wsgi
from djehuty.utils import convenience
import djehuty.backup.database as backup_database

# Even though we don't use these imports in 'ui', the state of
# SAML2_DEPENDENCY_LOADED is important to catch the situation
# in which this dependency is required due to the run-time configuration.
try:
    from onelogin.saml2.auth import OneLogin_Saml2_Auth  # pylint: disable=unused-import
    from onelogin.saml2.errors import OneLogin_Saml2_Error  # pylint: disable=unused-import
    SAML2_DEPENDENCY_LOADED = True
except (ImportError, ModuleNotFoundError):
    SAML2_DEPENDENCY_LOADED = False


# The 'uwsgi' module only needs to be available when deploying using uwsgi.
# To catch potential run-time problems early on in the situation that the
# uwsgi module is required, we set UWSGI_DEPENDENCY_LOADED here without
# actually using the module itself. It merely provides a mechanism to detect
# a run-time configuration error.
try:
    import uwsgi  # pylint: disable=unused-import
    UWSGI_DEPENDENCY_LOADED = True
except ModuleNotFoundError:
    UWSGI_DEPENDENCY_LOADED = False

class ConfigFileNotFound(Exception):
    """Raised when the database is not queryable."""

class UnsupportedSAMLProtocol(Exception):
    """Raised when an unsupported SAML protocol is used."""

class DependencyNotAvailable(Exception):
    """Raised when a required software dependency isn't available."""

class MissingConfigurationError(Exception):
    """Raised when a crucial piece of configuration is missing."""

def config_value (xml_root, path, command_line=None, fallback=None):
    """Procedure to get the value a config item should have at run-time."""

    ## Prefer command-line arguments.
    if command_line:
        return command_line

    ## Read from the configuration file.
    if xml_root:
        item = xml_root.find(path)
        if item is not None:
            return item.text

    ## Fall back to the fallback value.
    return fallback

def read_quotas_configuration (server, xml_root):
    """Read quota information from XML_ROOT."""

    quotas = xml_root.find("quotas")
    if not quotas:
        return None

    # Set the default quota for non-members.
    try:
        server.db.default_quota = int(quotas.attrib["default"])
    except (ValueError, TypeError):
        pass

    # Set specific quotas for member institutions and accounts.
    for quota_specification in quotas:
        email  = quota_specification.attrib.get("email")
        domain = quota_specification.attrib.get("domain")
        value  = None
        try:
            value = int(quota_specification.text)
        except (ValueError, TypeError):
            pass

        # Organization-wide quotas
        if domain is not None:
            server.db.group_quotas[domain] = value

        # Account-specific quotas
        elif email is not None:
            server.db.account_quotas[email] = value

    return None

def read_saml_configuration (server, xml_root):
    """Read the SAML configuration from XML_ROOT."""

    saml = xml_root.find("authentication/saml")
    if not saml:
        return None

    saml_version = None
    if "version" in saml.attrib:
        saml_version = saml.attrib["version"]

    if saml_version != "2.0":
        logging.error ("Only SAML 2.0 is supported.")
        raise UnsupportedSAMLProtocol

    saml_strict = bool(int(config_value (saml, "strict", None, True)))
    saml_debug  = bool(int(config_value (saml, "debug", None, False)))

    ## Service Provider settings
    service_provider     = saml.find ("service-provider")
    if service_provider is None:
        logging.error ("Missing service-provider information for SAML.")

    saml_sp_x509         = config_value (service_provider, "x509-certificate")
    saml_sp_private_key  = config_value (service_provider, "private-key")

    ## Service provider metadata
    sp_metadata          = service_provider.find ("metadata")
    if sp_metadata is None:
        logging.error ("Missing service provider's metadata for SAML.")

    organization_name    = config_value (sp_metadata, "display-name")
    organization_url     = config_value (sp_metadata, "url")

    sp_tech_contact      = sp_metadata.find ("./contact[@type='technical']")
    if sp_tech_contact is None:
        logging.error ("Missing technical contact information for SAML.")
    sp_tech_email        = config_value (sp_tech_contact, "email")
    if sp_tech_email is None:
        sp_tech_email = "-"

    sp_admin_contact     = sp_metadata.find ("./contact[@type='administrative']")
    if sp_admin_contact is None:
        logging.error ("Missing administrative contact information for SAML.")
    sp_admin_email        = config_value (sp_admin_contact, "email")
    if sp_admin_email is None:
        sp_admin_email = "-"

    sp_support_contact   = sp_metadata.find ("./contact[@type='support']")
    if sp_support_contact is None:
        logging.error ("Missing support contact information for SAML.")
    sp_support_email        = config_value (sp_support_contact, "email")
    if sp_support_email is None:
        sp_support_email = "-"

    ## Identity Provider settings
    identity_provider    = saml.find ("identity-provider")
    if identity_provider is None:
        logging.error ("Missing identity-provider information for SAML.")

    saml_idp_entity_id   = config_value (identity_provider, "entity-id")
    saml_idp_x509        = config_value (identity_provider, "x509-certificate")

    sso_service          = identity_provider.find ("single-signon-service")
    if sso_service is None:
        logging.error ("Missing SSO information of the identity-provider for SAML.")

    saml_idp_sso_url     = config_value (sso_service, "url")
    saml_idp_sso_binding = config_value (sso_service, "binding")

    server.identity_provider = "saml"

    ## Create an almost-ready-to-serialize configuration structure.
    ## The SP entityId will and ACS URL be generated at a later time.
    server.saml_config = {
        "strict": saml_strict,
        "debug":  saml_debug,
        "sp": {
            "entityId": None,
            "assertionConsumerService": {
                "url": None,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
            },
            "singleLogoutService": {
                "url": None,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
            "x509cert": saml_sp_x509,
            "privateKey": saml_sp_private_key
        },
        "idp": {
            "entityId": saml_idp_entity_id,
            "singleSignOnService": {
                "url": saml_idp_sso_url,
                "binding": saml_idp_sso_binding
            },
            "singleLogoutService": {
                "url": None,
                "binding": None
            },
            "x509cert": saml_idp_x509
        },
        "security": {
            "nameIdEncrypted": False,
            "authnRequestsSigned": True,
            "logoutRequestSigned": True,
            "logoutResponseSigned": True,
            "signMetadata": True,
            "wantMessagesSigned": False,
            "wantAssertionsSigned": False,
            "wantNameId" : True,
            "wantNameIdEncrypted": False,
            "wantAssertionsEncrypted": False,
            "allowSingleLabelDomains": False,
            "signatureAlgorithm": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
            "digestAlgorithm": "http://www.w3.org/2001/04/xmlenc#sha256",
            "rejectDeprecatedAlgorithm": True
        },
        "contactPerson": {
            "technical": {
                "givenName": "Technical support",
                "emailAddress": sp_tech_email
            },
            "support": {
                "givenName": "General support",
                "emailAddress": sp_support_email
            },
            "administrative": {
                "givenName": "Administrative support",
                "emailAddress": sp_admin_email
            }
        },
        "organization": {
            "nl": {
                "name": organization_name,
                "displayname": organization_name,
                "url": organization_url
            },
            "en": {
                "name": organization_name,
                "displayname": organization_name,
                "url": organization_url
            }
        }
    }

    del saml_sp_x509
    del saml_sp_private_key
    return None

def setup_saml_service_provider (server):
    """Write the SAML configuration file to disk and set up its metadata."""
    ## python3-saml wants to read its configuration from a file,
    ## but unfortunately we can only indicate the directory for that
    ## file.  Therefore, we create a separate directory in the cache
    ## for this purpose and place the file in that directory.
    if server.identity_provider == "saml":
        if not SAML2_DEPENDENCY_LOADED:
            logging.error ("Missing python3-saml dependency.")
            logging.error ("Cannot initiate authentication with SAML.")
            raise DependencyNotAvailable

        saml_cache_dir = os.path.join(server.db.cache.storage, "saml-config")
        os.makedirs (saml_cache_dir, mode=0o700, exist_ok=True)
        if os.path.isdir (saml_cache_dir):
            filename  = os.path.join (saml_cache_dir, "settings.json")
            saml_base_url = f"{server.base_url}/saml"
            saml_idp_binding = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
            # pylint: disable=unsubscriptable-object
            # PyLint assumes server.saml_config is None, but we can be certain
            # it contains a saml configuration, because otherwise
            # server.identity_provider wouldn't be set to "saml".
            server.saml_config["sp"]["entityId"] = saml_base_url
            server.saml_config["sp"]["assertionConsumerService"]["url"] = f"{saml_base_url}/login"
            server.saml_config["idp"]["singleSignOnService"]["binding"] = saml_idp_binding
            # pylint: enable=unsubscriptable-object
            config_fd = os.open (filename, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with open (config_fd, "w", encoding="utf-8") as file_stream:
                json.dump(server.saml_config, file_stream)
            server.saml_config_path = saml_cache_dir
        else:
            logging.error ("Failed to create '%s'.", saml_cache_dir)

def read_privilege_configuration (server, xml_root):
    """Read the privileges configureation from XML_ROOT."""
    privileges = xml_root.find("privileges")
    if not privileges:
        return None

    for account in privileges:
        try:
            email = account.attrib["email"]
            orcid = None
            if "orcid" in account.attrib:
                orcid = account.attrib["orcid"]

            server.db.privileges[email] = {
                "may_administer":  bool(int(config_value (account, "may-administer", None, False))),
                "may_impersonate": bool(int(config_value (account, "may-impersonate", None, False))),
                "may_review":      bool(int(config_value (account, "may-review", None, False))),
                "may_review_quotas": bool(int(config_value (account, "may-review-quotas", None, False))),
                "orcid":           orcid
            }
        except KeyError as error:
            logging.error ("Missing %s attribute for a privilege configuration.", error)
        except ValueError as error:
            logging.error ("Privilege configuration error: %s", error)

    return None

def configure_file_logging (log_file, inside_reload):
    """Procedure to set up logging to a file."""
    is_writeable = False
    log_file     = os.path.abspath (log_file)
    try:
        with open (log_file, "a", encoding = "utf-8"):
            is_writeable = True
    except (PermissionError, FileNotFoundError):
        pass

    if not is_writeable:
        if not inside_reload:
            logging.warning ("Cannot write to '%s'.", log_file)
    else:
        file_handler = logging.FileHandler (log_file, 'a')
        if not inside_reload:
            logging.info ("Writing further messages to '%s'.", log_file)

        formatter    = logging.Formatter('[ %(levelname)s ] %(asctime)s: %(message)s')
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        logger       = logging.getLogger()
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        logger.addHandler(file_handler)

def read_menu_configuration (xml_root, server, inside_reload):
    """Procedure to parse the menu configuration from XML_ROOT."""
    menu = xml_root.find("menu")
    if not menu:
        return None

    for primary_menu_item in menu:
        submenu = []
        for submenu_item in primary_menu_item:
            if submenu_item.tag == "sub-menu":
                submenu.append({
                    "title": config_value (submenu_item, "title"),
                    "href":  config_value (submenu_item, "href")
                })

        server.menu.append({
            "title":   config_value (primary_menu_item, "title"),
            "submenu": submenu
        })

    if not inside_reload:
        logging.info("Menu structure loaded")

    return None

def read_static_pages (static_pages, server, inside_reload, config_dir):
    """Procedure to parse and register static pages."""
    for page in static_pages:
        uri_path        = config_value (page, "uri-path")
        filesystem_path = config_value (page, "filesystem-path")
        redirect_to     = page.find("redirect-to")

        if uri_path is not None and filesystem_path is not None:
            if not os.path.isabs(filesystem_path):
                # take filesystem_path relative to config_dir and turn into absolute path
                filesystem_path = os.path.abspath(os.path.join(config_dir, filesystem_path))

            server.static_pages[uri_path] = {"filesystem-path": filesystem_path}
            if not inside_reload:
                logging.info ("Added static page: %s -> %s", uri_path, filesystem_path)
                logging.info("Related filesystem path: %s", filesystem_path)

        if uri_path is not None and redirect_to is not None:
            code = 302
            if "code" in redirect_to.attrib:
                code = int(redirect_to.attrib["code"])
            server.static_pages[uri_path] = {"redirect-to": redirect_to.text, "code": code}
            if not inside_reload:
                logging.info ("Added static page: %s", uri_path)
                logging.info ("Related redirect-to (%i) page: %s ", code, redirect_to.text)

def read_datacite_configuration (server, xml_root):
    """Procedure to parse and set the DataCite API configuration."""
    datacite = xml_root.find("datacite")
    if datacite:
        server.datacite_url      = config_value (datacite, "api-url")
        server.datacite_id       = config_value (datacite, "repository-id")
        server.datacite_password = config_value (datacite, "password")
        server.datacite_prefix   = config_value (datacite, "prefix")

def read_orcid_configuration (server, xml_root):
    """Procedure to parse and set the ORCID API configuration."""
    orcid = xml_root.find("authentication/orcid")
    if orcid:
        server.orcid_client_id     = config_value (orcid, "client-id")
        server.orcid_client_secret = config_value (orcid, "client-secret")
        server.orcid_endpoint      = config_value (orcid, "endpoint")
        server.identity_provider   = "orcid"

def read_email_configuration (server, xml_root):
    """Procedure to parse and set the email server configuration."""
    email = xml_root.find("email")
    if email:
        try:
            server.email.smtp_port = int(config_value (email, "port"))
            server.email.smtp_server = config_value (email, "server")
            server.email.from_address = config_value (email, "from")
            server.email.smtp_username = config_value (email, "username")
            server.email.smtp_password = config_value (email, "password")
            server.email.do_starttls = bool(int(config_value (email, "starttls", None, 0)))
        except ValueError:
            logging.error ("Could not configure the email subsystem:")
            logging.error ("The email port should be a numeric value.")

def read_configuration_file (server, config_file, address, port, state_graph,
                             storage, cache, base_url, use_debugger,
                             use_reloader):
    """Procedure to parse a configuration file."""

    inside_reload = os.environ.get('WERKZEUG_RUN_MAIN')
    try:
        config   = {}
        xml_root = None
        tree = ElementTree.parse(config_file)

        if config_file is not None and not inside_reload:
            logging.info ("Reading config file: %s", config_file)

        xml_root = tree.getroot()
        if xml_root.tag != "djehuty":
            raise ConfigFileNotFound

        config_dir = os.path.dirname(config_file)
        log_file = config_value (xml_root, "log-file", None, None)
        if log_file is not None:
            configure_file_logging (log_file, inside_reload)

        config["address"]       = config_value (xml_root, "bind-address", address, "127.0.0.1")
        config["port"]          = int(config_value (xml_root, "port", port, 8080))
        server.base_url         = config_value (xml_root, "base-url", base_url,
                                                f"http://{config['address']}:{config['port']}")
        server.in_production    = bool(int(config_value (xml_root, "production", None,
                                                         server.in_production)))
        server.db.storage       = config_value (xml_root, "storage-root", storage)
        server.db.cache.storage = config_value (xml_root, "cache-root", cache,
                                                f"{server.db.storage}/cache")
        server.db.endpoint      = config_value (xml_root, "rdf-store/sparql-uri")
        server.db.state_graph   = config_value (xml_root, "rdf-store/state-graph", state_graph)
        config["use_reloader"]  = config_value (xml_root, "live-reload", use_reloader)
        config["use_debugger"]  = config_value (xml_root, "debug-mode", use_debugger)
        config["maximum_workers"] = int(config_value (xml_root, "maximum-workers", None, 1))

        if config["use_reloader"]:
            config["use_reloader"] = bool(int(config["use_reloader"]))
        if config["use_debugger"]:
            config["use_debugger"] = bool(int(config["use_debugger"]))

        if not xml_root:
            return config

        secondary_storage = config_value (xml_root, "secondary-storage-root")
        if secondary_storage:
            server.db.secondary_storage = secondary_storage

        use_x_forwarded_for = bool(int(config_value (xml_root, "use-x-forwarded-for", None, 0)))
        if use_x_forwarded_for:
            server.log_access = server.log_access_using_x_forwarded_for

        read_orcid_configuration (server, xml_root)
        read_datacite_configuration (server, xml_root)
        read_email_configuration (server, xml_root)
        read_saml_configuration (server, xml_root)
        read_privilege_configuration (server, xml_root)
        read_quotas_configuration (server, xml_root)

        for include_element in xml_root.iter('include'):
            include    = include_element.text

            if include is None:
                continue

            if not os.path.isabs(include):
                include = os.path.join(config_dir, include)

            new_config = read_configuration_file (server,
                                                  include,
                                                  config["address"],
                                                  config["port"],
                                                  server.db.state_graph,
                                                  server.db.storage,
                                                  server.db.cache.storage,
                                                  server.base_url,
                                                  config["use_debugger"],
                                                  config["use_reloader"])
            config = { **config, **new_config }

        read_menu_configuration (xml_root, server, inside_reload)

        static_pages = xml_root.find("static-pages")
        if not static_pages:
            return config

        resources_root = config_value (static_pages, "resources-root", None, None)
        if not os.path.isabs(resources_root):
            # take resources_root relative to config_dir and turn into absolute path
            resources_root = os.path.abspath(os.path.join(config_dir, resources_root))
        if (server.add_static_root ("/s", resources_root) and not inside_reload):
            logging.info ("Added static root: %s", resources_root)

        read_static_pages (static_pages, server, inside_reload, config_dir)

        return config

    except ConfigFileNotFound:
        if not inside_reload:
            logging.error ("%s does not look like a Djehuty configuration file.",
                           config_file)
    except ElementTree.ParseError:
        if not inside_reload:
            logging.error ("%s does not contain valid XML.", config_file)
    except FileNotFoundError as error:
        if not inside_reload:
            logging.error ("Could not open '%s'.", config_file)
        raise SystemExit from error
    except UnsupportedSAMLProtocol as error:
        raise SystemExit from error

    return {}

## ----------------------------------------------------------------------------
## Starting point for the command-line program
## ----------------------------------------------------------------------------

def main (address=None, port=None, state_graph=None, storage=None,
          base_url=None, config_file=None, use_debugger=False,
          use_reloader=False, run_internal_server=True, initialize=True):
    """The main entry point for the 'web' subcommand."""
    try:
        convenience.add_logging_level ("ACCESS", logging.INFO + 5)
        convenience.add_logging_level ("STORE", logging.INFO + 4)
        server = wsgi.ApiServer ()
        config = read_configuration_file (server, config_file, address, port,
                                          state_graph, storage, None, base_url,
                                          use_debugger, use_reloader)

        inside_reload = os.environ.get('WERKZEUG_RUN_MAIN')

        if not server.db.cache.cache_is_ready() and not inside_reload:
            logging.error("Failed to set up cache layer.")

        setup_saml_service_provider (server)

        if not server.in_production and not inside_reload:
            logging.warning ("Assuming to run in a non-production environment.")
            logging.warning (("Set <production> to 1 in your configuration "
                              "file for hardened security settings."))

        if server.in_production and not os.path.isdir (server.db.storage):
            logging.error ("The storage directory '%s' does not exist.",
                           server.db.storage)
            raise FileNotFoundError

        if not run_internal_server:
            server.using_uwsgi = True
            return server

        if inside_reload:
            logging.info("Reloaded.")
        else:
            if not server.menu:
                logging.warning ("No menu structure provided.")

            if shutil.which ("git") is None:
                logging.error("Cannot find the 'git' executable.  Please install it.")
                if server.in_production:
                    raise DependencyNotAvailable

            logging.info("State graph:  %s.", server.db.state_graph)
            logging.info("Storage path: %s.", server.db.storage)
            logging.info("Secondary storage path: %s.", server.db.secondary_storage)
            logging.info("Cache storage path: %s.", server.db.cache.storage)
            logging.info("Running on %s", server.base_url)

            if server.identity_provider is not None:
                logging.info ("Using %s as identity provider.",
                              server.identity_provider.upper())
            else:
                logging.error ("Missing identity provider configuration.")
                if server.in_production:
                    raise MissingConfigurationError

            if not server.email.is_properly_configured ():
                logging.warning ("Sending notification e-mails has been disabled.")
                logging.warning ("Please configure a mail server to enable it.")

            if initialize:
                logging.info("Invalidating caches ...")
                server.db.cache.invalidate_all ()
                logging.info("Initializing RDF store ...")
                rdf_store = backup_database.DatabaseInterface()

                if not rdf_store.insert_static_triplets ():
                    logging.error ("Failed to gather static triplets")

                if server.db.add_triples_from_graph (rdf_store.store):
                    logging.info("Initialization completed.")

                server.db.initialize_privileged_accounts ()
                initialize = False

        run_simple (config["address"], config["port"], server,
                    threaded=(config["maximum_workers"] <= 1),
                    processes=config["maximum_workers"],
                    use_debugger=config["use_debugger"],
                    use_reloader=config["use_reloader"])

    except (FileNotFoundError, DependencyNotAvailable, MissingConfigurationError):
        pass

    return None

## ----------------------------------------------------------------------------
## Starting point for uWSGI
## ----------------------------------------------------------------------------

def application (env, start_response):
    """The main entry point for the WSGI."""

    logging.basicConfig(format='[ %(levelname)s ] %(asctime)s: %(message)s',
                        level=logging.INFO)
    convenience.add_logging_level ("ACCESS", logging.INFO + 5)
    convenience.add_logging_level ("STORE", logging.INFO + 4)

    if not UWSGI_DEPENDENCY_LOADED:
        start_response('500 Internal Server Error', [('Content-Type','text/html')])
        return [b"<p>Cannot find the <code>uwsgi</code> Python module.</p>"]

    config_file = os.getenv ("DJEHUTY_CONFIG_FILE")

    if config_file is None:
        start_response('200 OK', [('Content-Type','text/html')])
        return [b"<p>Please set the <code>DJEHUTY_CONFIG_FILE</code> environment variable.</p>"]

    server = main (config_file=config_file, run_internal_server=False)
    return server (env, start_response)
