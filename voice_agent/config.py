"""One agent configuration plus one assignment, snapshotted for each call."""
from dataclasses import dataclass
import json
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo

from .caller import unknown_caller
from .ending import END_CALL_TOOL

from .paths import ROOT
DEFAULT_CONFIG = ROOT / 'agent.toml'

# Reject misspellings instead of silently keeping an unexpected prompt/model.
FIELDS = {
    'agent': {'default_assignment': str, 'timezone': str, 'instructions': str},
    'voice': {'model': str, 'voice': str, 'instructions': str},
    'delegation': {'model': str, 'reasoning_effort': str, 'max_output_tokens': int,
                   'tool_choice': str, 'parallel_tool_calls': bool, 'instructions': str},
    'conversation': {'incoming_greeting': str, 'outgoing_greeting': str},
    'ending': {'delay_seconds': int},
    'summary': {'model': str, 'reasoning_effort': str, 'max_output_tokens': int, 'instructions': str},
}


@dataclass(frozen=True)
class AgentConfig:
    path: Path
    data: dict

    @property
    def default_assignment(self):
        return (self.path.parent / self.data['agent']['default_assignment']).resolve()

    @property
    def timezone(self):
        return ZoneInfo(self.data['agent']['timezone'])

    @property
    def models(self):
        return tuple(dict.fromkeys(self.data[k]['model'] for k in ('voice', 'delegation', 'summary')))


def load_config(path=DEFAULT_CONFIG):
    path = Path(path).resolve()
    with path.open('rb') as source:
        data = tomllib.load(source)
    if set(data) != set(FIELDS):
        raise ValueError('Agent config must contain exactly: ' + ', '.join(FIELDS))
    for section, fields in FIELDS.items():
        values = data[section]
        if not isinstance(values, dict) or set(values) != set(fields):
            raise ValueError(f'Invalid fields in [{section}]; expected: ' + ', '.join(fields))
        for name, kind in fields.items():
            value = values[name]
            if type(value) is not kind or (kind is str and not value.strip()):
                raise ValueError(f'Invalid {section}.{name}')
        if 'max_output_tokens' in fields and values['max_output_tokens'] < 16:
            raise ValueError(f'{section}.max_output_tokens must be at least 16')
    if data['delegation']['tool_choice'] not in ('auto', 'required', 'none'):
        raise ValueError('delegation.tool_choice must be auto, required or none')
    if not 1 <= data['ending']['delay_seconds'] <= 30:
        raise ValueError('ending.delay_seconds must be between 1 and 30 seconds')
    config = AgentConfig(path, data)
    config.timezone  # Validate before opening a phone or API connection.
    return config


def load_call_settings(config_path, assignment_path=None):
    agent = load_config(config_path)
    path = Path(assignment_path).resolve() if assignment_path is not None else agent.default_assignment
    assignment = path.read_text()
    if not assignment.strip():
        raise ValueError('Assignment prompt is empty')
    return agent, assignment, path


def configuration(agent, assignment, now, caller=None, *, outgoing_number=None, direction=None):
    """Build both system prompts from the same assignment and factual context."""
    if not assignment.strip():
        raise ValueError('Assignment prompt is empty')
    context = {'local_time': now,
               'direction': direction or ('outgoing' if outgoing_number is not None else 'incoming'),
               'dialed_number': outgoing_number,
               'caller_id': caller if caller is not None else unknown_caller(),
               'caller_id_revision': 0}
    shared = (agent.data['agent']['instructions'].strip() + '\n\nOwner assignment:\n' + assignment.strip()
              + '\n\nCall context (JSON):\n' + json.dumps(context, ensure_ascii=True))
    voice, backend = agent.data['voice'], agent.data['delegation']
    return {
        'model': voice['model'], 'store': False,
        'instructions': voice['instructions'].strip() + '\n\n' + shared,
        'audio': {'output': {'voice': voice['voice']}},
        'delegation': {'type': 'responses', 'responses': {
            **response_settings(backend),
            'instructions': backend['instructions'].strip() + '\n\n' + shared,
            'tools': [END_CALL_TOOL],
            'tool_choice': backend['tool_choice'],
            'parallel_tool_calls': backend['parallel_tool_calls'],
        }},
    }


def response_settings(section):
    return {'model': section['model'], 'reasoning': {'effort': section['reasoning_effort']},
            'max_output_tokens': section['max_output_tokens']}
