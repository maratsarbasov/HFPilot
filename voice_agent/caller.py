"""Transport facts, not identity verification or caller-specific business rules."""
import json


def unknown_caller():
    return {'number': None, 'name': None, 'number_type': None,
            'source': None, 'status': 'unknown'}


def caller_context(caller, revision):
    return json.dumps({'caller_id': caller, 'caller_id_revision': revision}, ensure_ascii=True)
