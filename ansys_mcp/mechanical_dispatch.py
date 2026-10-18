# This file is also executed verbatim inside Mechanical's IronPython 2.7 runtime.
# Keep it independent of the CPython host package, annotations, and f-strings.
"""Public API object dispatcher and typed JSON wire format for Mechanical."""
import json
import math


class BridgeError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


try:
    _string_types = (basestring,)  # noqa: F821 - IronPython 2.7
    _integer_types = (int, long)  # noqa: F821
except NameError:
    _string_types = (str,)
    _integer_types = (int,)


def _identifier(name):
    if not isinstance(name, _string_types) or not name or name.startswith('_'):
        raise BridgeError('MECHANICAL_INVALID_MEMBER', 'Only public API members can be addressed.')
    if not all(c.isalnum() or c == '_' for c in name) or name[0].isdigit():
        raise BridgeError('MECHANICAL_INVALID_MEMBER', 'Invalid API member.')
    return name


def _resolve_path(path, namespace):
    parts = path.split('.')
    for part in parts:
        _identifier(part)
    allowed_roots = ('Ansys', 'System', 'ExtAPI', 'DataModel', 'Model', 'Graphics',
                     'Tree', 'Quantity', 'Transaction', 'SelectionTypeEnum',
                     'DataModelObjectCategory', 'MechanicalUnitSystem',
                     'ViewOrientationType', 'Point', 'Vector3D', 'EngineeringData', 'EngData')
    if parts[0] not in allowed_roots:
        raise BridgeError('MECHANICAL_UNKNOWN_ROOT', 'Root is outside the public Mechanical API namespace.')
    if parts[0] == 'System' and len(parts) > 1:
        safe_system_types = ('Boolean', 'Byte', 'SByte', 'Int16', 'UInt16', 'Int32',
                             'UInt32', 'Int64', 'UInt64', 'Single', 'Double',
                             'Decimal', 'String', 'Object', 'Array')
        safe_collections = ('System.Collections.Generic.List', 'System.Collections.Generic.Dictionary',
                            'System.Collections.Generic.IList', 'System.Collections.Generic.IEnumerable',
                            'System.Collections.Generic.ICollection', 'System.Collections.Generic.KeyValuePair')
        if not (parts[1] in safe_system_types and len(parts) == 2) and path != 'System.Data.DataTable' and path not in safe_collections:
            raise BridgeError('MECHANICAL_TRUSTED_SCRIPT_REQUIRED', 'This .NET API requires an explicit trusted script file.')
    if parts[0] not in namespace:
        try:
            namespace[parts[0]] = __import__(parts[0])
        except ImportError:
            raise BridgeError('MECHANICAL_UNKNOWN_ROOT', 'Unknown public API root: ' + parts[0])
    result = namespace[parts[0]]
    for part in parts[1:]:
        result = getattr(result, part)
    return result


def _resolve_type(path, namespace):
    if path.endswith('[]'):
        return _resolve_path('System.Array', namespace)[_resolve_type(path[:-2], namespace)]
    if path.endswith('&'):
        import clr
        return clr.GetClrType(_resolve_type(path[:-1], namespace)).MakeByRefType()
    if '[' not in path:
        return _resolve_path(path, namespace)
    base, arguments = path.split('[', 1)
    arguments = arguments[:-1]
    depth, start, parts = 0, 0, []
    for index, character in enumerate(arguments):
        if character == '[':
            depth += 1
        elif character == ']':
            depth -= 1
        elif character == ',' and depth == 0:
            parts.append(arguments[start:index].strip())
            start = index + 1
    parts.append(arguments[start:].strip())
    types = tuple(_resolve_type(part, namespace) for part in parts)
    factory = _resolve_path(base, namespace)
    return factory[types[0] if len(types) == 1 else types]


def _type_name(value):
    try:
        return str(value.GetType().FullName)
    except (AttributeError, TypeError):
        cls = type(value)
        return str(getattr(cls, '__module__', '')) + '.' + str(getattr(cls, '__name__', 'object'))


def _reference(value, state):
    # Identity is retained only within this session and generation.
    for key, original in state['refs'].items():
        if original is value:
            return {'$type': 'ref', 'session_id': state['session_id'], 'namespace': state['context'],
                    'generation': state['generation'], 'object_id': key, 'type': _type_name(value)}
    state['sequence'] += 1
    key = 'object_' + str(state['sequence'])
    state['refs'][key] = value
    return {'$type': 'ref', 'session_id': state['session_id'], 'namespace': state['context'],
            'generation': state['generation'], 'object_id': key, 'type': _type_name(value)}


def _dereference(value, state):
    if (value['session_id'] != state['session_id'] or value['generation'] != state['generation']
            or value.get('namespace', 'mechanical') != state['context']):
        raise BridgeError('MECHANICAL_STALE_REFERENCE', 'Object reference belongs to a different session or model generation.')
    key = value['object_id']
    if key not in state['refs']:
        raise BridgeError('MECHANICAL_STALE_REFERENCE', 'Object reference is no longer present.')
    result = state['refs'][key]
    # Deleted DataModel objects may still exist as CLR wrappers. Reject them.
    try:
        object_id = result.ObjectId
    except AttributeError:
        object_id = None
    if object_id is not None:
        model = state['namespace'].get('DataModel')
        if model is not None and hasattr(model, 'GetObjectById'):
            current = model.GetObjectById(object_id)
            if current is None:
                raise BridgeError('MECHANICAL_STALE_REFERENCE', 'The referenced model object has been deleted.')
            same = current is result
            if not same:
                try:
                    system = _resolve_path('System', state['namespace'])
                    same = bool(system.Object.ReferenceEquals(current, result))
                except (AttributeError, BridgeError):
                    same = False
            if not same:
                raise BridgeError('MECHANICAL_STALE_REFERENCE', 'The model object identity changed; query a new reference.')
    return result


def _quantity(value, unit, namespace):
    constructor = _resolve_path('Quantity', namespace)
    return constructor(str(value) + ' [' + str(unit) + ']')


def _decode(value, state):
    namespace = state['namespace']
    if isinstance(value, list):
        return [_decode(item, state) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get('$type')
    if kind == 'ref':
        return _dereference(value, state)
    if kind == 'local_ref':
        name = value['name']
        if name not in state['named']:
            raise BridgeError('MECHANICAL_UNKNOWN_REFERENCE', 'Unknown named result: ' + name)
        result = state['named'][name]
        if 'index' in value:
            result = result[value['index']]
        return _decode(result, state)
    if kind == 'quantity':
        return _quantity(value['value'], value['unit'], namespace)
    if kind == 'quantity_series':
        return [_quantity(item, value['unit'], namespace) for item in value['values']]
    if kind == 'enum':
        enum_type = _resolve_path(value['type'], namespace)
        if 'value' in value:
            import clr
            system = _resolve_path('System', namespace)
            return system.Enum.ToObject(clr.GetClrType(enum_type), value['value'])
        return getattr(enum_type, _identifier(value['name']))
    if kind == 'selection':
        enum_type = namespace.get('SelectionTypeEnum')
        if enum_type is None:
            enum_type = _resolve_path('Ansys.ACT.Interfaces.Common.SelectionTypeEnum', namespace)
        selection = namespace['ExtAPI'].SelectionManager.CreateSelectionInfo(getattr(enum_type, value['kind']))
        selection.Ids = list(value['ids'])
        return selection
    if kind == 'path':
        return value['path']
    if kind == 'table':
        # The DataTable representation is explicit; Mechanical field variables use
        # quantity_series for Input/Output.DiscreteValues instead.
        system = _resolve_path('System', namespace)
        try:
            import clr
            clr.AddReference('System.Data')
        except ImportError:
            pass
        table = _resolve_path('System.Data.DataTable', namespace)()
        for column in value['columns']:
            added = table.Columns.Add(column['name'], system.Object)
            if column.get('unit'):
                added.ExtendedProperties['unit'] = column['unit']
        for row in value['rows']:
            record = table.NewRow()
            for index, item in enumerate(row):
                column = value['columns'][index]
                record[index] = _quantity(item, column['unit'], namespace) if column.get('unit') and not isinstance(item, dict) else _decode(item, state)
            table.Rows.Add(record)
        return table
    if kind:
        raise BridgeError('MECHANICAL_INVALID_VALUE', 'Unknown typed value.')
    return dict((key, _decode(item, state)) for key, item in value.items())


def _encode(value, state, depth=0):
    if value is None or isinstance(value, (bool,) + _string_types + _integer_types):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise BridgeError('MECHANICAL_NONFINITE_RESULT', 'Result contains a nonfinite number.')
        return value
    if depth > 24:
        return _reference(value, state)
    if isinstance(value, dict):
        return dict((str(key), _encode(item, state, depth + 1)) for key, item in value.items())
    type_name = _type_name(value)
    if type_name.endswith('.Quantity'):
        return {'$type': 'quantity', 'value': float(value.Value), 'unit': str(value.Unit)}
    try:
        if value.GetType().IsEnum:
            return {'$type': 'enum', 'type': type_name, 'name': str(value), 'value': int(value)}
    except (AttributeError, TypeError):
        pass
    if type_name == 'System.Data.DataTable':
        columns = []
        for column in value.Columns:
            metadata = {'name': str(column.ColumnName)}
            if column.ExtendedProperties.ContainsKey('unit'):
                metadata['unit'] = str(column.ExtendedProperties['unit'])
            columns.append(metadata)
        return {'$type': 'table', 'columns': columns,
                'rows': [[_encode(row[index], state, depth + 1) for index in range(len(columns))] for row in value.Rows]}
    if isinstance(value, (list, tuple)) or type_name.startswith('System.Collections.') or type_name.endswith('[]'):
        return [_encode(item, state, depth + 1) for item in value]
    return _reference(value, state)


def _describe(value, limit):
    names = [name for name in dir(value) if not name.startswith('_')]
    names.sort()
    result = {'type': _type_name(value), 'members': names[:limit], 'member_count': len(names),
              'truncated': len(names) > limit, 'methods': [], 'constructors': []}
    try:
        runtime_type = value.GetType()
        for method in runtime_type.GetMethods():
            if method.IsPublic:
                result['methods'].append({'name': str(method.Name), 'signature': str(method)})
        for constructor in runtime_type.GetConstructors():
            if constructor.IsPublic:
                result['constructors'].append(str(constructor))
    except (AttributeError, TypeError):
        # IronPython reflected type objects expose public overload documentation.
        result['documentation'] = str(getattr(value, '__doc__', '') or '')[:16000]
    result['methods'] = result['methods'][:limit]
    result['constructors'] = result['constructors'][:limit]
    return result


def _target(operation, state):
    value = operation.get('target', 'Model')
    if value == 'System':
        raise BridgeError('MECHANICAL_TRUSTED_SCRIPT_REQUIRED', 'Address an explicit supported .NET value type.')
    resolver = _resolve_type if operation['op'] == 'construct' else _resolve_path
    result = resolver(value, state['namespace']) if isinstance(value, _string_types) else _decode(value, state)
    type_name = _type_name(result)
    if (type_name.startswith(('System.IO.', 'System.Diagnostics.', 'System.Reflection.', 'Microsoft.Win32.'))
            or (type_name in ('System.RuntimeType', 'System.Type') and not isinstance(value, _string_types))):
        raise BridgeError('MECHANICAL_TRUSTED_SCRIPT_REQUIRED', 'Reflection and OS APIs require an explicit trusted script file.')
    return result


def _invalidate(state):
    state['generation'] += 1
    state['refs'].clear()
    state['named'].clear()


def _operation(operation, state):
    op = operation['op']
    if op == 'call' and operation.get('member') in (
            'SendCommand', 'ExecuteCommand', 'ExecuteScript', 'RunScript',
            'RunPythonScript', 'ScriptByName', 'ExecutePythonScript', 'Execute',
            'RunCommand', 'RunMacro', 'Evaluate', 'EvaluateExpression'):
        raise BridgeError('MECHANICAL_TRUSTED_SCRIPT_REQUIRED', 'Script execution APIs require an explicit trusted script file.')
    target = None
    if op in ('discover', 'inspect', 'get', 'set', 'call', 'construct'):
        target = _target(operation, state)
    if op == 'set' and operation.get('member') in ('Text', 'Code', 'Script', 'ScriptText', 'ScriptBody', 'Input'):
        if any(word in _type_name(target) for word in ('PythonCode', 'PythonResult', 'CommandSnippet', 'Script')):
            raise BridgeError('MECHANICAL_TRUSTED_SCRIPT_REQUIRED', 'Script bodies require an explicit trusted script file.')
    if op in ('discover', 'inspect'):
        return _describe(target, operation.get('limit', 200))
    if op == 'get':
        return getattr(target, _identifier(operation['member']))
    if op == 'set':
        member = _identifier(operation['member'])
        setattr(target, member, _decode(operation.get('value'), state))
        return getattr(target, member)
    if op in ('call', 'construct'):
        callable_value = getattr(target, _identifier(operation['member'])) if op == 'call' else target
        if operation.get('overload') is not None:
            types = tuple(_resolve_type(name, state['namespace']) for name in operation['overload'])
            overloads = getattr(callable_value, 'Overloads', None)
            if overloads is None:
                overloads = getattr(callable_value, '__overloads__', None)
            if overloads is None:
                raise BridgeError('MECHANICAL_OVERLOAD_UNAVAILABLE', 'The product does not expose explicit overload selection for this callable.')
            callable_value = overloads[types]
        args = _decode(operation.get('args', []), state)
        kwargs = _decode(operation.get('kwargs', {}), state)
        return callable_value(*args, **kwargs)
    if op == 'find':
        model = state['namespace']['DataModel']
        scoped = operation.get('target', 'Model') != 'Model' or not operation.get('recursive', True)
        category = _decode(operation['category'], state) if operation.get('category') is not None else None
        if scoped:
            root = _target(operation, state)
            found = []
            pending = list(getattr(root, 'Children', []))
            visited = set()
            while pending:
                item = pending.pop(0)
                identity = id(item)
                if identity in visited:
                    continue
                visited.add(identity)
                if (operation.get('name') is None or str(getattr(item, 'Name', '')) == operation['name']) and (category is None or getattr(item, 'DataModelObjectCategory', None) == category):
                    found.append(item)
                if operation.get('recursive', True):
                    pending.extend(list(getattr(item, 'Children', [])))
        elif operation.get('name') is not None:
            found = list(model.GetObjectsByName(operation['name']))
            if category is not None:
                found = [item for item in found if getattr(item, 'DataModelObjectCategory', None) == category]
        else:
            found = list(model.GetObjectsByType(category))
        limit = operation.get('limit', 200)
        if len(found) > limit:
            raise BridgeError('MECHANICAL_RESULT_LIMIT', 'Find exceeded the requested limit; refine the query or increase limit.')
        return found
    if op == 'script':
        path = operation['script_path']
        stream = open(path, 'rb')
        try:
            source = stream.read().decode('utf-8-sig')
        finally:
            stream.close()
        namespace = state['namespace']
        result_name = operation.get('result_name', 'mcp_result')
        namespace.pop(result_name, None)
        eval(compile(source, path, 'exec'), namespace, namespace)
        return namespace.get(result_name)
    raise BridgeError('MECHANICAL_UNSUPPORTED_OPERATION', 'This operation must be handled by the host backend.')


def dispatch(request, namespace):
    session_id = request['session_id']
    generation = request['generation']
    context = request.get('namespace', 'mechanical')
    states = namespace.setdefault('_MCP_BRIDGES', {})
    state = states.get(context)
    if state is None or state['session_id'] != session_id or state.get('context') != context:
        state = {'session_id': session_id, 'context': context, 'generation': generation, 'refs': {}, 'named': {}, 'sequence': 0}
        states[context] = state
    if state['generation'] != generation:
        state['generation'] = generation
        state['refs'].clear()
        state['named'].clear()
    state['namespace'] = namespace
    # Public roots differ between Mechanical script engines. Resolve once, using
    # the documented global ExtAPI when Model/DataModel aliases are absent.
    if 'ExtAPI' in namespace:
        namespace['DataModel'] = namespace['ExtAPI'].DataModel
        namespace['Model'] = namespace['ExtAPI'].DataModel.Project.Model
    operation = request['operation']
    try:
        qualified_name = None
        if operation['op'] in ('discover', 'inspect', 'get', 'set', 'call', 'construct'):
            api_target = _target(operation, state)
            qualified_name = _type_name(api_target)
            if operation.get('member'):
                qualified_name += '.' + operation['member']
        value = _operation(operation, state)
        structural_call = operation['op'] == 'call' and operation.get('member') in (
            'Open', 'New', 'Import', 'ImportGeometry', 'Refresh', 'Update', 'Delete',
            'DeleteAllChildren', 'Reset')
        if operation.get('invalidate_refs') or operation['op'] == 'script' or structural_call:
            _invalidate(state)
        if operation.get('store_as'):
            state['named'][operation['store_as']] = _encode(value, state)
        result = {'ok': True, 'generation': state['generation'], 'value': _encode(value, state),
                  'qualified_api_name': qualified_name, 'namespace': context,
                  'backend': request.get('backend'), 'product_version': request.get('product_version')}
    except Exception as exc:
        error_code = getattr(exc, 'code', None)
        if not isinstance(error_code, _string_types):
            error_code = 'MECHANICAL_API_ERROR'
        result = {'ok': False, 'generation': state['generation'],
                  'error': {'code': error_code,
                            'message': str(exc), 'exception_type': _type_name(exc)}}
    text = json.dumps(result, ensure_ascii=True, allow_nan=False)
    if len(text.encode('utf-8')) > request.get('max_inline_bytes', 262144):
        path = request.get('artifact_path')
        if not path:
            return json.dumps({'ok': False, 'generation': state['generation'],
                               'error': {'code': 'MECHANICAL_OUTPUT_TOO_LARGE', 'message': 'A result artifact path is required.'}})
        stream = open(path, 'w')
        try:
            stream.write(text)
        finally:
            stream.close()
        envelope = {'ok': result['ok'], 'generation': state['generation'],
                    'artifact': {'path': path, 'format': 'json', 'size': len(text.encode('utf-8'))}}
        for key in ('qualified_api_name', 'namespace', 'backend', 'product_version'):
            if key in result:
                envelope[key] = result[key]
        if not result['ok']:
            error = result.get('error', {})
            envelope['error'] = {'code': error.get('code', 'MECHANICAL_API_ERROR'),
                                 'message': error.get('message', '')[:256]}
        return json.dumps(envelope)
    return text


if '_MCP_REQUEST_JSON' in globals():
    _MCP_RESPONSE_JSON = dispatch(json.loads(_MCP_REQUEST_JSON), globals())
