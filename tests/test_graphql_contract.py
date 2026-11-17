"""Check the integration's GraphQL against the operations the official app sends.

There is no public schema (introspection needs a login), so the official Lyse
Lading app stands in for one, since it works against the live API. Each
operation in api.py must match an app operation on the same root field, pass
arguments of the same shape, and select only fields the app also selects.

When this fails after an app update, re-extract the fixture with
scripts/extract_app_graphql.py and adjust the queries in api.py to match.
"""

from __future__ import annotations

from pathlib import Path
import re

import graphql
from graphql import (
    FieldNode,
    ObjectValueNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
    ValueNode,
    VariableNode,
)
import pytest

from custom_components.lyse_charge import api

APP_OPERATIONS = Path(__file__).parent / "fixtures" / "lyse_lading_app.graphql"

type RootKey = tuple[OperationType, str]


def _operations(source: str) -> list[OperationDefinitionNode]:
    return [d for d in graphql.parse(source).definitions if isinstance(d, OperationDefinitionNode)]


def _root_fields(operation: OperationDefinitionNode) -> list[FieldNode]:
    return [s for s in operation.selection_set.selections if isinstance(s, FieldNode)]


def _selected_paths(selection_set: SelectionSetNode | None, prefix: str = "") -> set[str]:
    """Return every selected field as a dotted path, e.g. {"sockets", "sockets.id"}."""
    paths: set[str] = set()
    for selection in selection_set.selections if selection_set else ():
        if isinstance(selection, FieldNode):
            path = f"{prefix}{selection.name.value}"
            paths.add(path)
            paths |= _selected_paths(selection.selection_set, f"{path}.")
    return paths


def _argument_shape(value: ValueNode, operation: OperationDefinitionNode) -> object:
    """Describe an argument by structure and types, ignoring variable names.

    `chargersByIds(chargerIds: $ids)` and `chargersByIds(chargerIds: $chargerIds)`
    are equivalent if both variables are `[String!]!`; `startCharging(input: $input)`
    with `$input: StartChargingInput!` is not equivalent to an inline object.
    """
    if isinstance(value, VariableNode):
        for definition in operation.variable_definitions or ():
            if definition.variable.name.value == value.name.value:
                return ("variable", graphql.print_ast(definition.type))
        raise AssertionError(f"Undeclared variable ${value.name.value}")
    if isinstance(value, ObjectValueNode):
        return ("object", {f.name.value: _argument_shape(f.value, operation) for f in value.fields})
    return ("literal", graphql.print_ast(value))


def _arguments(field: FieldNode, operation: OperationDefinitionNode) -> dict[str, object]:
    return {a.name.value: _argument_shape(a.value, operation) for a in field.arguments or ()}


def _app_index() -> dict[RootKey, tuple[OperationDefinitionNode, FieldNode]]:
    index: dict[RootKey, tuple[OperationDefinitionNode, FieldNode]] = {}
    for operation in _operations(APP_OPERATIONS.read_text()):
        for field in _root_fields(operation):
            index.setdefault((operation.operation, field.name.value), (operation, field))
    return index


INTEGRATION_QUERIES = {
    name: value for name, value in vars(api).items() if re.fullmatch(r"_GQL_[A-Z_]+", name) and isinstance(value, str)
}


def test_integration_queries_found() -> None:
    """Guard the parametrized test below against silently matching nothing."""
    assert len(INTEGRATION_QUERIES) >= 7


@pytest.mark.parametrize("name", sorted(INTEGRATION_QUERIES))
def test_matches_app_operation(name: str) -> None:
    """Each integration operation is a subset of the app's operation on the same root field."""
    app = _app_index()
    (operation,) = _operations(INTEGRATION_QUERIES[name])

    for field in _root_fields(operation):
        key = (operation.operation, field.name.value)
        assert key in app, f"{name}: the app has no {key[0].value} on root field {key[1]!r}"
        app_operation, app_field = app[key]

        assert _arguments(field, operation) == _arguments(app_field, app_operation), (
            f"{name}: arguments to {field.name.value} differ from the app's {app_operation.name.value if app_operation.name else 'operation'}"
        )

        unknown = _selected_paths(field.selection_set) - _selected_paths(app_field.selection_set)
        assert not unknown, f"{name}: selects fields the app does not: {sorted(unknown)}"


def test_fixture_comes_from_a_known_app_version() -> None:
    """The fixture header records which app build it came from, so drift is traceable."""
    header = APP_OPERATIONS.read_text().splitlines()[0]
    assert re.match(r"# GraphQL operations extracted from Lyse Lading \d+\.\d+\.\d+ \(build \d+\)\.", header)
