# -*- coding: utf-8
import os
import sys
import traceback
import warnings

from sqlalchemy import exc as sa_exc

from globaleaks import models, DATABASE_VERSION
from globaleaks.handlers.admin.https import load_tls_dict_list
from globaleaks.models import Base, Config
from globaleaks.models.config_desc import ConfigFilters
from globaleaks.orm import get_engine, get_session, make_db_uri, transact, transact_sync
from globaleaks.settings import Settings
from globaleaks.state import State, TenantState
from globaleaks.utils import fs
from globaleaks.utils.log import log
from globaleaks.utils.objectdict import ObjectDict


def get_db_file(db_path):
    """
    Utility function to retrieve the database file path
    :param db_path: The path where to look for the database file
    :return: The version and the path of the existing database file
    """
    path = os.path.join(db_path, 'globaleaks.db')
    if os.path.exists(path):
        session = get_session(make_db_uri(path))
        version_db = session.query(models.Config.value).filter(Config.tid == 1,
                                                               Config.var_name == 'version_db').one()[0]
        session.close()
        return version_db, path

    for i in reversed(range(0, DATABASE_VERSION + 1)):
        file_name = 'glbackend-%d.db' % i
        db_file_path = os.path.join(db_path, 'db', file_name)
        if os.path.exists(db_file_path):
            return i, db_file_path

    return 0, ''


def create_db():
    """
    Utility function to create a new database
    """
    engine = get_engine()
    engine.execute('PRAGMA foreign_keys = ON')
    engine.execute('PRAGMA secure_delete = ON')
    engine.execute('PRAGMA auto_vacuum = FULL')
    engine.execute('PRAGMA automatic_index = ON')
    Base.metadata.create_all(engine)


def compact_db():
    """
    Execute VACUUM command to deallocate database space
    """
    engine = get_engine()
    engine.execute('VACUUM')


@transact_sync
def init_db(session):
    """
    Transaction for initializing the application database
    :param session: An ORM session
    """
    from globaleaks.handlers.admin import tenant
    tenant.db_create(session, {'active': True, 'mode': 'default', 'name': 'GLOBALEAKS', 'subdomain': ''})


def update_db():
    """
    This function handles the update of an existing database
    :return: The database version
    """
    db_version, db_file_path = get_db_file(Settings.working_path)
    if db_version == 0:
        return 0

    try:
        with warnings.catch_warnings():
            from globaleaks.db import migration
            warnings.simplefilter("ignore", category=sa_exc.SAWarning)

            log.err('Found an already initialized database version: %d', db_version)

            if db_version != DATABASE_VERSION:
                log.err('Performing schema migration from version %d to version %d',
                        db_version, DATABASE_VERSION)

                migration.perform_migration(db_version)
            else:
                migration.perform_data_update(db_file_path)
                compact_db()

    except Exception as exception:
        log.err('Failure: %s', exception)
        log.err('Verbose exception traceback:')
        etype, value, tback = sys.exc_info()
        log.info('\n'.join(traceback.format_exception(etype, value, tback)))
        return -1

    return DATABASE_VERSION


def db_get_tracked_files(session):
    """
    Transaction for retrieving the list of attachment files tracked by the application database
    :param session: An ORM session
    :return: The list of filenames of the attachment files
    """
    ifiles = list(session.query(models.InternalFile.filename).distinct())
    rfiles = list(session.query(models.ReceiverFile.filename).distinct())
    wbfiles = list(session.query(models.WhistleblowerFile.filename).distinct())

    return [x[0] for x in ifiles + rfiles + wbfiles]


@transact_sync
def sync_clean_untracked_files(session):
    """
    Transaction for removing files that are not tracked by the application database
    :param session: An ORM session
    """
    tracked_files = db_get_tracked_files(session)
    for filesystem_file in os.listdir(Settings.attachments_path):
        if filesystem_file not in tracked_files:
            file_to_remove = os.path.join(Settings.attachments_path, filesystem_file)
            log.debug('Removing untracked file: %s', file_to_remove)
            try:
                fs.srm(file_to_remove)
            except OSError:
                log.err('Failed to remove untracked file', file_to_remove)


@transact_sync
def sync_initialize_snimap(session):
    """
    Transaction for loading TLS certificates and initialize the SNI map
    :param session: An ORM session
    """
    for cfg in load_tls_dict_list(session):
        if cfg['https_enabled']:
            State.snimap.load(cfg['tid'], cfg)


def db_refresh_tenant_cache(session, tids=None):
    active_tids = set([tid[0] for tid in session.query(models.Tenant.id).filter(models.Tenant.active.is_(True))])

    cached_tids = set(State.tenant_state.keys())

    # Remove tenants that have been disabled
    for tid in cached_tids - active_tids:
        if tid in State.tenant_state:
            del State.tenant_state[tid]

        if tid in State.tenant_cache:
            del State.tenant_cache[tid]

    if tids is None or 1 in tids:
        tids = active_tids
    else:
        tids = [tid for tid in tids if tid in active_tids]

    tids = sorted(tids)

    for tid in tids:
        if tid not in State.tenant_state:
            State.tenant_state[tid] = TenantState(State)

        if tid not in State.tenant_cache:
            State.tenant_cache[tid] = ObjectDict()

        tenant_cache = State.tenant_cache[tid]

        tenant_cache['ip_filter'] = {}
        tenant_cache['https_allowed'] = {}
        tenant_cache['redirects'] = {}
        tenant_cache['custodian'] = False
        tenant_cache['notification'] = ObjectDict()
        tenant_cache['notification'].admin_list = []
        tenant_cache['hostnames'] = []
        tenant_cache['onionnames'] = []

    root_tenant_cache = State.tenant_cache[1]

    for tid, lang in session.query(models.EnabledLanguage.tid, models.EnabledLanguage.name)\
                            .filter(models.EnabledLanguage.tid.in_(tids)):
        State.tenant_cache[tid].setdefault('languages_enabled', []).append(lang)

    for cfg in session.query(Config).filter(Config.tid.in_(tids)):
        tenant_cache = State.tenant_cache[cfg.tid]

        if cfg.var_name in ConfigFilters['node']:
            tenant_cache[cfg.var_name] = cfg.value
        elif cfg.var_name in ConfigFilters['notification']:
            tenant_cache['notification'][cfg.var_name] = cfg.value

    for tid, mail, pub_key in session.query(models.User.tid, models.User.mail_address, models.User.pgp_key_public) \
                                     .filter(models.User.state == 'enabled',
                                             models.User.role == 'admin',
                                             models.User.notification.is_(True),
                                             models.User.tid.in_(tids)):
        State.tenant_cache[tid].notification.admin_list.extend([(mail, pub_key)])

    for custodian in session.query(models.User) \
                            .filter(models.User.role == 'custodian',
                                    models.User.state == 'enabled',
                                    models.User.tid.in_(tids)):
        State.tenant_cache[custodian.tid]['custodian'] = True

    for redirect in session.query(models.Redirect).filter(models.Redirect.tid.in_(tids)):
        State.tenant_cache[redirect.tid]['redirects'][redirect.path1] = redirect.path2

    for tid in tids:
        tenant_cache = State.tenant_cache[tid]

        for x in [('admin', 'ip_filter_admin_enable', 'ip_filter_admin'),
                  ('custodian', 'ip_filter_custodian_enable', 'ip_filter_custodian'),
                  ('receiver', 'ip_filter_receiver_enable', 'ip_filter_receiver')]:
            if tenant_cache[x[1]]:
                tenant_cache['ip_filter'][x[0]] = tenant_cache[x[2]]

        for x in ['admin', 'custodian', 'receiver', 'whistleblower']:
            tenant_cache['https_allowed'][x] = State.tenant_cache[tid].get('https_' + x, True)

        if State.tenant_cache[tid].mode == 'whistleblowing.it':
            tenant_cache['https_preload'] = State.tenant_cache[1]['https_preload']

        if tenant_cache.hostname and tenant_cache.reachable_via_web:
            tenant_cache.hostnames.append(tenant_cache.hostname.encode())

        if tenant_cache.onionservice:
            tenant_cache.onionnames.append(tenant_cache.onionservice.encode())

        if not tenant_cache.onionservice and root_tenant_cache.onionservice:
            tenant_cache.onionservice = tenant_cache.subdomain + '.' + root_tenant_cache.onionservice

        if tenant_cache.subdomain:
            if root_tenant_cache.rootdomain and tenant_cache.reachable_via_web:
                tenant_cache.hostnames.append('{}.{}'.format(tenant_cache.subdomain, root_tenant_cache.rootdomain).encode())

            if root_tenant_cache.onionservice:
                tenant_cache.onionnames.append('{}.{}'.format(tenant_cache.subdomain, root_tenant_cache.onionservice).encode())

    tenant_uuid_id_map = {}
    tenant_hostname_id_map = {}
    tenant_subdomain_id_map = {}

    for tid in active_tids:
        tenant_cache = State.tenant_cache[tid]
        tenant_uuid_id_map[tenant_cache.uuid] = tid

        tenant_hostname_id_map.update({h: tid for h in tenant_cache.hostnames + tenant_cache.onionnames})

        if tenant_cache.subdomain:
            tenant_subdomain_id_map[tenant_cache.subdomain] = tid

    State.tenant_uuid_id_map = tenant_uuid_id_map
    State.tenant_hostname_id_map = tenant_hostname_id_map
    State.tenant_subdomain_id_map = tenant_subdomain_id_map

    if 1 in tids:
        log.setloglevel(State.tenant_cache[1].log_level)


@transact
def refresh_tenant_cache(session, tids=None):
    return db_refresh_tenant_cache(session, tids)


@transact_sync
def sync_refresh_tenant_cache(session, tids=None):
    return db_refresh_tenant_cache(session, tids)
