
# -*- coding: utf-8 -*-
#
# This file is part of Zenodo.
# Copyright (C) 2015, 2016 CERN.
#
# Zenodo is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# Zenodo is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Zenodo; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place, Suite 330, Boston,
# MA 02111-1307, USA.
#
# In applying this license, CERN does not
# waive the privileges and immunities granted to it by virtue of its status
# as an Intergovernmental Organization or submit itself to any jurisdiction.

"""Record transformation and normalization."""

from __future__ import absolute_import, print_function

from datetime import datetime
from functools import reduce

from invenio_communities.errors import InclusionRequestExistsError
from invenio_communities.models import Community, InclusionRequest
from invenio_db import db
from invenio_oaiserver.response import datetime_to_datestamp
from invenio_pidstore.models import PersistentIdentifier, PIDStatus
from invenio_records.api import Record
from invenio_records_files.models import RecordsBuckets
from six import string_types
from sqlalchemy.orm.exc import NoResultFound


def migrate_record(record_uuid, logger=None):
    """Migrate a record."""
    try:
        # Migrate record
        record = Record.get_record(record_uuid)
        if '$schema' in record:
            if logger:
                logger.info("Record already migrated.")
            return
        record = transform_record(record)
        record.commit()
        # Create provisional communities.
        if 'provisional_communities' in record:
            for c_id in record['provisional_communities']:
                try:
                    c = Community.get(c_id)
                    if c:
                        InclusionRequest.create(c, record, notify=False)
                    else:
                        if logger:
                            logger.warning(
                                "Community {0} does not exists "
                                "(record {1}).".format(
                                    c_id, str(record.id)))
                except InclusionRequestExistsError:
                    if logger:
                        logger.warning("Inclusion request exists.")
            del record['provisional_communities']
        # Create RecordsBuckets entries
        bucket_ids = {file['bucket'] for file in record.get('files', [])}
        for bucket_id in bucket_ids:
            db.session.add(
                RecordsBuckets(record_id=record.id, bucket_id=bucket_id)
            )

        db.session.commit()
    except NoResultFound:
        if logger:
            logger.info("Deleted record - no migration required.")
    except Exception:
        db.session.rollback()
        pid = PersistentIdentifier.get_by_object('recid', 'rec', record_uuid)
        pid.status = PIDStatus.RESERVED
        db.session.commit()
        raise


def transform_record(record):
    """Transform legacy JSON."""
    # Record is already migrated.
    if '$schema' in record:
        return record

    transformations = [
        _remove_fields,
        _migrate_upload_type,
        _migrate_authors,
        _migrate_oai,
        _migrate_grants,
        _migrate_meetings,
        _migrate_owners,
        _migrate_description,
        _migrate_imprint,
        _migrate_references,
        _migrate_communities,
        _migrate_provisional_communities,
        _add_schema,
    ]

    return reduce(lambda record, func: func(record), transformations, record)


def _remove_fields(record):
    """Remove record."""
    keys = [
        'fft', 'files_to_upload', 'files_to_upload', 'collections',
        'preservation_score', 'restriction', 'url', 'version_history',
        'documents', 'creation_date', 'modification_date',
        'system_control_number', 'system_number',
    ]

    for k in keys:
        if k in record:
            del record[k]

    return record


def _migrate_description(record):
    if 'description' not in record:
        record['description'] = ''
    return record


def _migrate_imprint(record):
    """Transform upload type."""
    if 'imprint' not in record:
        return record

    record['part_of'] = dict()
    for k in ['publisher', 'title', 'year']:
        if k in record['imprint']:
            if k in record['part_of']:
                raise Exception("Cannot migrate imprint")
            record['part_of'][k] = record['imprint'][k]

    del record['imprint']
    return record


def _migrate_upload_type(record):
    """Transform upload type."""
    if 'upload_type' not in record:
        raise Exception(record)
    record['resource_type'] = record['upload_type']
    del record['upload_type']
    return record


def _migrate_authors(record):
    """Transform upload type."""
    record['creators'] = record['authors']
    for c in record['creators']:
        if isinstance(c.get('affiliation'), list):
            c['affiliation'] = c['affiliation'][0]
    del record['authors']
    return record


def _migrate_meetings(record):
    """Transform upload type."""
    if 'conference_url' in record:
        if 'meetings' not in record:
            record['meetings'] = dict()
        record['meetings']['url'] = record['conference_url']
        del record['conference_url']

    return record


def _migrate_owners(record):
    if 'owner' not in record:
        return record
    o = record['owner']
    del record['owner']

    record['owners'] = [int(o['id'])] if o.get('id') else []
    record['_internal'] = {
        'state': 'published',
        'source': {
            'legacy_deposit_id': o.get('deposition_id'),
            'agents': [{
                'role': 'uploader',
                'email': o.get('email'),
                'username': o.get('username'),
                'user_id': o.get('id'),
            }]
        }
    }

    for k in list(record['_internal']['source']['agents'][0].keys()):
        if not record['_internal']['source']['agents'][0][k]:
            del record['_internal']['source']['agents'][0][k]
    return record


def _migrate_grants(record):
    """Transform upload type."""
    if 'grants' not in record:
        return record

    def mapper(x):
        gid = 'http://dx.zenodo.org/grants/10.13039/501100000780::{0}'.format(
            x['identifier'])
        return {'$ref': gid}
    record['grants'] = [mapper(x) for x in record['grants']]
    return record


def _migrate_references(record):
    """Transform upload type."""
    if 'references' not in record:
        return record

    def mapper(x):
        return {'raw_reference': x['raw_reference']}

    record['references'] = [
        mapper(x) for x in record['references'] if x.get('raw_reference')]
    return record


def _migrate_oai(record):
    """Transform record OAI information."""
    if 'oai' not in record:
        return record

    oai = record.pop('oai')

    sets = oai.get('indicator', [])
    if isinstance(sets, string_types):
        sets = [sets]

    # OAI sets
    record['_oai'] = {
        'id': oai['oai'],
        'sets': sets,
        'updated': datetime_to_datestamp(datetime.utcnow()),
    }

    return record


def _migrate_communities(record):
    if 'communities' not in record:
        return record

    comms = record['communities']
    if isinstance(comms, string_types):
        comms = [comms]

    if comms:
        record['communities'] = list(set(comms))
    return record


def _migrate_provisional_communities(record):
    if 'provisional_communities' not in record:
        return record

    comms = record['provisional_communities']
    if isinstance(comms, string_types):
        comms = [comms]

    if comms:
        if 'communities' in record:
            record['provisional_communities'] = list(
                set(comms) - set(record['communities']))
        else:
            record['provisional_communities'] = list(set(comms))
    else:
        del record['provisional_communities']
    return record


def _add_schema(record):
    """Transform record OAI information."""
    record['$schema'] = 'https://zenodo.org/schemas/records/record-v1.0.0.json'
    return record