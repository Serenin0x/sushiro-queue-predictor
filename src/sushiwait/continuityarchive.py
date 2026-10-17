"""Separate public archive with transactional records and anonymous relations.

Existing one-table archives are rejected, never migrated or supplemented in
place. Plain v1 packets remain valid inputs, with unknown writer continuity.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3

from .capture import CaptureError
from .continuity import SEMANTICS, _LINK_KEYS, _uuid5, read_continuity_bundle, validate_continuity_bundle
from .monitoring import _stamp, _time
from .packets import encoded
from .receipts import PacketArchive, ReceiptError, _CREATE
from .storage import _unique_object

_CLAIMS = ('CREATE TABLE continuity_claims(observation_id TEXT PRIMARY KEY NOT NULL,'
    'writer_id TEXT NOT NULL,link_sha256 TEXT,link_json TEXT,claimed_at TEXT NOT NULL,linked_at TEXT,'
    'FOREIGN KEY(observation_id) REFERENCES archive_records(observation_id))')
_BUNDLES = ('CREATE TABLE continuity_bundles(bundle_sha256 TEXT PRIMARY KEY NOT NULL,'
    'bundle_json TEXT NOT NULL,archived_at TEXT NOT NULL)')
SCHEMA = (_CREATE, _CLAIMS, _BUNDLES)


def archive_bundle_file(path, database, *, as_of):
    if os.path.abspath(path) == os.path.abspath(database):
        raise ReceiptError('archive_path_conflict')
    bundle = read_continuity_bundle(path, as_of=as_of)
    with ContinuityArchive(database) as archive:
        return archive.append_bundle(bundle, as_of=as_of)


class ContinuityArchive(PacketArchive):
    _schema = SCHEMA

    def __init__(self, path):
        super().__init__(path)
        self.db.execute('PRAGMA foreign_keys=ON')

    def append_bundle(self, bundle, *, as_of):
        safe = validate_continuity_bundle(bundle, as_of=as_of)
        records = {r['observation_id']: r for r in safe['packet']['records']}
        writers = {v['observation_id']: v['writer_id'] for v in safe['links']}
        for anchor in safe['anchors']:
            record = anchor['record']
            records[record['observation_id']] = record
            writers[record['observation_id']] = anchor['writer_id']
        body = encoded(safe).decode('utf8')
        digest = hashlib.sha256(body.encode('utf8')).hexdigest()
        at = _stamp(_time(as_of))
        status = 'not_started'
        try:
            self._guard()
            self.db.execute('BEGIN IMMEDIATE')
            existing = self.db.execute('SELECT bundle_json,archived_at FROM continuity_bundles WHERE bundle_sha256=?',
                                       [digest]).fetchone()
            if existing is not None:
                if existing[0] != body:
                    raise ReceiptError('continuity_archive_bundle_conflict')
                if _stamp(_time(existing[1])) != existing[1] or _time(existing[1]) > _time(at):
                    raise ReceiptError('continuity_archive_bundle_time')
            for record in records.values():
                previous = self.db.execute('SELECT store_id,api_profile,data_origin,recorded_at,ok,archived_at '
                    'FROM archive_records WHERE observation_id=?', [record['observation_id']]).fetchone()
                if previous is not None and (previous[:4] != tuple(record[k] for k in
                        ('store_id','api_profile','data_origin','recorded_at'))
                        or type(previous[4]) is not int or previous[4] != int(record['ok'])
                        or _stamp(_time(previous[5])) != previous[5] or _time(previous[5]) > _time(at)):
                    raise ReceiptError('continuity_archive_record_columns')
            inserted, duplicates = self._insert_records(list(records.values()), as_of=at)
            # Declare all aliases before inserting links, including anchors
            # whose full link may arrive in a later page. An alias is immutable.
            existing_aliases=set()
            for identity, alias in writers.items():
                claim = self.db.execute('SELECT writer_id,claimed_at,linked_at,link_sha256,link_json '
                                        'FROM continuity_claims WHERE observation_id=?',
                                        [identity]).fetchone()
                if claim is None:
                    self.db.execute('INSERT INTO continuity_claims VALUES(?,?,NULL,NULL,?,NULL)',
                                    [identity, alias, at])
                else:
                    existing_aliases.add(identity)
                    if claim[0] != alias:
                        raise ReceiptError('continuity_archive_writer_conflict')
                    if (_stamp(_time(claim[1])) != claim[1] or _time(claim[1]) > _time(at)
                            or claim[2] is not None and (_stamp(_time(claim[2])) != claim[2]
                                or not _time(claim[1]) <= _time(claim[2]) <= _time(at))):
                        raise ReceiptError('continuity_archive_claim_time')
                    if ((claim[2] is None) != (claim[3] is None)
                            or (claim[2] is None) != (claim[4] is None)):
                        raise ReceiptError('continuity_archive_incomplete_claim')
            new_links = duplicate_links = 0
            completed_anchors=[]
            for link in safe['links']:
                raw = encoded(link).decode('utf8')
                current = self.db.execute('SELECT link_sha256,link_json FROM continuity_claims WHERE observation_id=?',
                                          [link['observation_id']]).fetchone()
                if current == (None, None):
                    self.db.execute('UPDATE continuity_claims SET link_sha256=?,link_json=?,linked_at=? WHERE observation_id=?',
                                    [link['link_sha256'], raw, at, link['observation_id']])
                    new_links += 1
                    if link['observation_id'] in existing_aliases:
                        completed_anchors.append(link['observation_id'])
                elif current == (link['link_sha256'], raw):
                    duplicate_links += 1
                else:
                    raise ReceiptError('continuity_archive_link_conflict')
            self._check_completed_anchor_cycles(completed_anchors)
            if existing is None:
                self.db.execute('INSERT INTO continuity_bundles VALUES(?,?,?)', [digest, body, at])
            self._guard()
            status = 'unknown'
            self.db.commit()
            status = 'committed'
            self._guard()
            os.fsync(self.parent_fd)
            return {'bundle_sha256': digest, 'inserted_records': inserted,
                    'duplicate_records': duplicates, 'record_count_including_anchors': len(records),
                    'linked_observations': len(safe['links']), 'inserted_links': new_links,
                    'duplicate_links': duplicate_links, 'durability_confirmed': True,
                    'archive_commit_status': status, 'continuity_archive_schema_version': 1,
                    'source_claims_verified': False, 'credentials_accessed': False,
                    'network_performed': False, 'eta_available': False}
        except ReceiptError as error:
            self.db.rollback()
            raise ReceiptError(error.error_code, commit_status=status) from None
        except sqlite3.Error as error:
            self.db.rollback()
            code=getattr(error,'sqlite_errorcode',None)
            if type(code) is int and (code & 255) in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED) and status!='committed':
                # A saved public reader may hold a shared lock at commit.
                # Keep the immutable pending bundle and retry its exact body;
                # never acknowledge or advance a cursor on this outcome.
                raise ReceiptError('continuity_archive_busy',commit_status=status) from None
            raise ReceiptError('continuity_archive_commit_unconfirmed' if status != 'not_started'
                               else 'continuity_archive_database_error', commit_status=status) from None
        except (OSError, CaptureError):
            self.db.rollback()
            raise ReceiptError('continuity_archive_commit_unconfirmed' if status != 'not_started'
                               else 'continuity_archive_database_error', commit_status=status) from None
        except (ValueError, TypeError, OverflowError):
            self.db.rollback()
            raise ReceiptError('continuity_archive_invalid_existing_claim', commit_status=status) from None

    def _check_completed_anchor_cycles(self, identities):
        """New identities cannot be old predecessors; completed anchors can.

        Check those newly completed paths inside the same transaction. A
        finite 20000-node traversal bound fails explicitly rather than making
        an unbounded history scan or accepting an unchecked long path.
        """
        finished=set()
        for identity in identities:
            current=identity;trail=set()
            while current is not None and current not in finished:
                if current in trail:
                    raise ReceiptError('continuity_archive_graph_cycle')
                if len(trail)>=20000:
                    raise ReceiptError('continuity_archive_graph_bound')
                trail.add(current)
                row=self.db.execute('SELECT link_sha256,CASE WHEN length(CAST(link_json AS BLOB))<=4096 '
                    'THEN link_json END FROM continuity_claims WHERE observation_id=?',[current]).fetchone()
                if row is None:
                    raise ReceiptError('continuity_archive_missing_predecessor')
                if row==(None,None):break
                link=json.loads(row[1],object_pairs_hook=_unique_object,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if (type(link) is not dict or set(link)!=_LINK_KEYS or link['observation_id']!=current
                        or link['link_sha256']!=row[0] or encoded(link).decode()!=row[1]
                        or hashlib.sha256(encoded({k:v for k,v in link.items() if k!='link_sha256'})).hexdigest()!=row[0]):
                    raise ReceiptError('continuity_archive_invalid_existing_link')
                current=link['predecessor_id']
                if current is not None:_uuid5(current)
            finished.update(trail)


def singleton_bundle(db, record, *, packet):
    """Validate one stored link with its exact predecessor in this transaction.

    None means no full relation is archived, never a successful empty stream.
    Column/body mismatches and missing references fail instead of hiding damage.
    """
    from .officialview import _json
    identity = record['observation_id']
    claim = db.execute('SELECT writer_id,link_sha256,CASE WHEN length(CAST(link_json AS BLOB))<=4096 THEN link_json END,'
                       'claimed_at,linked_at FROM continuity_claims WHERE observation_id=?',
                       [identity]).fetchone()
    if claim is None:
        return None
    alias, digest, raw, claimed, linked_at = claim
    _uuid5(alias)
    if _stamp(_time(claimed)) != claimed or _time(claimed) > _time(packet['as_of']):
        raise ReceiptError('continuity_archive_claim_time')
    if digest is None and raw is None and linked_at is None:
        return None
    if (_stamp(_time(linked_at)) != linked_at or _time(linked_at) > _time(packet['as_of'])):
        raise ReceiptError('continuity_archive_link_time')
    if _time(linked_at) < _time(claimed):
        raise ReceiptError('continuity_archive_link_time')
    link = _json(raw)
    if (type(link) is not dict or link.get('observation_id') != identity
            or link.get('record_sha256') != record['record_sha256']
            or link.get('writer_id') != alias or link.get('link_sha256') != digest):
        raise ReceiptError('continuity_archive_link_mismatch')
    anchors = []
    if link.get('predecessor_id') is not None:
        prior = db.execute('SELECT observation_id,record_sha256,store_id,api_profile,data_origin,recorded_at,ok,archived_at,'
                           'CASE WHEN length(CAST(record_json AS BLOB))<=65536 THEN record_json END '
                           'FROM archive_records WHERE observation_id=?',
                           [link['predecessor_id']]).fetchone()
        previous_claim = db.execute('SELECT writer_id,claimed_at FROM continuity_claims WHERE observation_id=?',
                                    [link['predecessor_id']]).fetchone()
        if (prior is None or previous_claim is None
                or prior[1] != link.get('predecessor_sha256')
                or previous_claim[0] != link.get('predecessor_writer_id')
                or _stamp(_time(previous_claim[1])) != previous_claim[1]
                or _time(previous_claim[1]) > _time(packet['as_of'])):
            raise ReceiptError('continuity_archive_predecessor_mismatch')
        previous_record = _json(prior[8])
        columns = ('observation_id','record_sha256','store_id','api_profile','data_origin','recorded_at')
        if (type(previous_record) is not dict or any(previous_record.get(k) != prior[i]
                for i,k in enumerate(columns)) or type(prior[6]) is not int or prior[6] not in (0,1)
                or previous_record.get('ok') is not bool(prior[6])
                or _stamp(_time(prior[7])) != prior[7] or _time(prior[7]) > _time(packet['as_of'])):
            raise ReceiptError('continuity_archive_predecessor_mismatch')
        anchors = [{'record': previous_record, 'writer_id': previous_claim[0]}]
    bundle = validate_continuity_bundle({'continuity_schema_version': 1,
        'packet': {**packet, 'records': [record]}, 'anchors': anchors, 'links': [link],
        'continuity_semantics': SEMANTICS, 'source_claims_verified': False}, as_of=packet['as_of'])
    return bundle, linked_at
