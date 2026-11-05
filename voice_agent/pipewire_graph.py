"""Graph helpers for the unsupported legacy PipeWire backend."""
import json
import subprocess


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=8)


def graph():
    return json.loads(run('pw-dump'))


def props(obj):
    return obj.get('info', {}).get('props', {})


def node_ports(objects, node_id, direction):
    return [o for o in objects if o['type'].endswith(':Port')
            and props(o).get('node.id') == node_id
            and o.get('info', {}).get('direction') == direction]


def link_name(objects, port):
    node = next(o for o in objects if o['id'] == props(port)['node.id'])
    return props(node)['node.name'] + ':' + props(port)['port.name']
