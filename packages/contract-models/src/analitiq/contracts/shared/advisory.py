"""Advisory (relational) rule engine — the single source for the cross-field tier.

Structural rules (types, enums, patterns, closed objects, discriminated unions)
are model fields and render into the published JSON Schema. The *relational*
rules — set-equality, disjointness, membership, cross-key uniqueness between
sibling instance values — cannot be expressed in stock JSON Schema. Rather than
scatter them across opaque `@model_validator` bodies (and hand-copy them into a
non-Python engine), they are authored ONCE as structured data in
``advisory_rules.py`` and enforced HERE by one generic validator.

The same registry drives two outputs, in sync by construction:

1. runtime enforcement — the :class:`AdvisoryValidated` mixin runs a model's
   registered rules on every ``model_validate``;
2. a stable ``id`` per rule keys, for the **generic** kinds, a valid/invalid
   instance fixture corpus (``contract-models/tests/fixtures/advisory``) a
   non-Python second system can re-implement the fixed rule *kinds* against and
   reconcile. ``kind="custom"`` rules are enforced only in-process by their
   named validator (they may carry no fixtures), so their logic is not portable
   to a non-Python engine — the registry entry keeps the census complete, not
   re-implementable.

A rule whose logic is irreducibly bespoke (recursive schema walks, hash
equality, path resolution) is still *catalogued* here with ``kind="custom"`` and
an ``enforcer`` naming the method that keeps enforcing it — so the census stays
complete without faking generality.

This module imports no contract models: rules bind to their target classes by
*name*, so the data can be imported by tooling without pulling in pydantic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from typing import Any, Callable

from pydantic import model_validator

# --- Rule kinds -------------------------------------------------------------

#: The fixed, generically-checkable relational vocabulary. Each maps to exactly
#: one checker in ``_CHECKERS``. ``custom`` is the escape hatch: catalogued only,
#: enforced by the method named in ``AdvisoryRule.enforcer``.
GENERIC_KINDS = (
    "disjoint",
    "set_equal",
    "member_of",
    "subset_of",
    "unique_by",
)
CUSTOM_KIND = "custom"


# --- Rule datum -------------------------------------------------------------


@dataclass(frozen=True)
class AdvisoryRule:
    """One relational contract rule, authored as data.

    ``fields`` are *field expressions* (see :func:`resolve`); their meaning is
    kind-specific and documented on each checker. ``targets`` are the model
    class names the rule binds to (matched against the whole MRO, so a rule on a
    base class covers its subclasses). ``options`` carries kind-specific knobs
    (e.g. ``case_insensitive``, ``key``). ``enforcer`` is set only for
    ``kind="custom"`` and names the bespoke method that enforces it.
    """

    id: str
    kind: str
    resource: str
    prose: str
    targets: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)
    enforcer: str | None = None
    #: Concrete model class the shared fixtures validate against (defaults to the
    #: first target; set explicitly when the first target is an abstract base).
    fixture_model: str | None = None

    def __post_init__(self) -> None:
        if self.kind == CUSTOM_KIND:
            if not self.enforcer:
                raise ValueError(f"{self.id}: custom rule must name an enforcer")
        elif self.kind not in GENERIC_KINDS:
            raise ValueError(f"{self.id}: unknown rule kind {self.kind!r}")
        if not self.targets:
            raise ValueError(f"{self.id}: rule must bind at least one target")

    @property
    def fixture_target(self) -> str:
        """The concrete model name the fixture corpus validates against."""
        return self.fixture_model or self.targets[0]


# --- Field-expression resolver ---------------------------------------------
#
# A deliberately small, closed grammar — enough for every relational rule, and
# no arbitrary code:
#   "enum"                        attribute (scalar or collection)
#   "ui.options"                  dotted attribute chain, None-safe
#   "destinations[]"             a list attribute (the elements)
#   "ui.options[].value"         project ``.value`` over each list element
# Exactly one ``[]`` segment is supported (all rules need at most one level).


def _get_path(obj: Any, path: str) -> Any:
    """Follow a dotted attribute chain, short-circuiting to None on any gap."""
    cur = obj
    for seg in path.split("."):
        if cur is None:
            return None
        cur = getattr(cur, seg, None)
    return cur


def resolve(model: Any, expr: str) -> Any:
    """Resolve a field expression against a model instance.

    Returns the attribute value, or — for a projection (``a[].b``) — a list of
    the projected values. Any missing/None link yields None so relational rules
    can uniformly skip absent operands.
    """
    if "[]" in expr:
        head, _, tail = expr.partition("[]")
        seq = _get_path(model, head.rstrip(".")) if head.strip(".") else model
        if seq is None:
            return None
        tail = tail.lstrip(".")
        return list(seq) if not tail else [_get_path(el, tail) for el in seq]
    return _get_path(model, expr)


def _as_set(value: Any, *, case_insensitive: bool = False) -> set | None:
    """Coerce an operand to a set of comparable members, or None to skip.

    A dict contributes its keys; a list/tuple its items. Empty and None both
    mean "operand absent" — every relational rule below relates two operands
    only when both are actually present, matching the imperative validators.
    """
    if not value:
        return None
    items = list(value.keys()) if isinstance(value, dict) else list(value)
    if case_insensitive:
        items = [m.lower() if isinstance(m, str) else m for m in items]
    return set(items)


# --- Generic checkers -------------------------------------------------------
#
# Each raises ValueError (pydantic wraps it) on violation, or returns quietly.
# The message always leads with the rule id so failures are greppable and the
# fixture corpus can assert on a stable token.


def _msg(rule: AdvisoryRule, detail: str) -> str:
    return f"[{rule.id}] {rule.prose} ({detail})"


def _check_disjoint(rule: AdvisoryRule, model: Any) -> None:
    """``fields = (a, b)``: the members of a and b must not overlap.

    Dict operands contribute keys. ``options.case_insensitive`` casefolds.
    """
    ci = rule.options.get("case_insensitive", False)
    a = _as_set(resolve(model, rule.fields[0]), case_insensitive=ci)
    b = _as_set(resolve(model, rule.fields[1]), case_insensitive=ci)
    if a is None or b is None:
        return
    overlap = sorted(a & b)
    if overlap:
        raise ValueError(_msg(rule, f"overlap={overlap!r}"))


def _check_set_equal(rule: AdvisoryRule, model: Any) -> None:
    """``fields = (a, b)``: a and b must contain exactly the same members.

    Compared element-wise (not via ``set``) so unhashable members — an ``enum``
    of objects/arrays, say — are tolerated exactly as the imperative validators
    were. Absent/empty operands skip; ``a`` is resolved first so ``b`` is never
    coerced when ``a`` is absent.
    """
    a = resolve(model, rule.fields[0])
    if not a:
        return
    b = resolve(model, rule.fields[1])
    if not b:
        return
    extra = [x for x in a if x not in b]
    missing = [x for x in b if x not in a]
    if extra or missing:
        raise ValueError(_msg(rule, f"extra={extra!r}; missing={missing!r}"))


def _check_member_of(rule: AdvisoryRule, model: Any) -> None:
    """``fields = (needle, haystack)``: needle must be a member of haystack.

    Membership is element-wise (``in`` over the collection), tolerating an
    unhashable needle or member as the imperative validator did.
    """
    needle = resolve(model, rule.fields[0])
    haystack = resolve(model, rule.fields[1])
    if needle is None or not haystack:
        return
    if needle not in haystack:
        raise ValueError(_msg(rule, f"value={needle!r} not in {list(haystack)!r}"))


def _check_subset_of(rule: AdvisoryRule, model: Any) -> None:
    """``fields = (sub, sup)``: every member of sub must appear in sup."""
    sub = _as_set(resolve(model, rule.fields[0]))
    sup = _as_set(resolve(model, rule.fields[1]))
    if sub is None or sup is None:
        return
    extra = sorted(sub - sup)
    if extra:
        raise ValueError(_msg(rule, f"not declared: {extra!r}"))


def find_duplicates(seq: Any, key: Callable[[Any], Any] | None = None) -> list:
    """Return the sorted, de-duplicated keys that appear more than once in seq.

    The one uniqueness primitive: the generic ``unique_by`` checker and the thin
    ``_check_unique_destinations`` shim both call it, so the algorithm is defined
    exactly once.
    """
    seen: set = set()
    dups: set = set()
    for el in seq or ():
        k = key(el) if key else el
        if k in seen:
            dups.add(k)
        else:
            seen.add(k)
    return sorted(dups)


def _check_unique_by(rule: AdvisoryRule, model: Any) -> None:
    """``fields = (seq,)``: elements of seq must be unique.

    ``options.key`` (list of dotted subpaths) projects each element to a tuple
    for the comparison; omit it to compare whole elements (scalar lists).
    ``options.skip_null`` drops elements whose key is (or contains) None from the
    comparison — for uniqueness over an optional field, where absent is not a
    collision.
    """
    seq = resolve(model, rule.fields[0])
    if not seq:
        return
    key_paths = rule.options.get("key")
    key_fn = (lambda el: tuple(_get_path(el, kp) for kp in key_paths)) if key_paths else None
    if rule.options.get("skip_null"):
        keys = [key_fn(el) if key_fn else el for el in seq]
        keys = [k for k in keys if k is not None and not (isinstance(k, tuple) and None in k)]
        dups = find_duplicates(keys)
    else:
        dups = find_duplicates(seq, key_fn)
    if dups:
        raise ValueError(_msg(rule, f"duplicates={dups!r}"))


_CHECKERS: dict[str, Callable[[AdvisoryRule, Any], None]] = {
    "disjoint": _check_disjoint,
    "set_equal": _check_set_equal,
    "member_of": _check_member_of,
    "subset_of": _check_subset_of,
    "unique_by": _check_unique_by,
}


def check_rule(rule: AdvisoryRule, model: Any) -> None:
    """Enforce one rule against a model instance (no-op for custom rules)."""
    checker = _CHECKERS.get(rule.kind)
    if checker is not None:
        checker(rule, model)


# --- Registry + mixin -------------------------------------------------------

_RULES_BY_TARGET: dict[str, list[AdvisoryRule]] = {}
_ALL_RULES: list[AdvisoryRule] = []
def register(rules: list[AdvisoryRule]) -> None:
    """Index a batch of rules by every target class they bind to."""
    for rule in rules:
        _ALL_RULES.append(rule)
        for target in rule.targets:
            _RULES_BY_TARGET.setdefault(target, []).append(rule)


@cache
def _ensure_loaded() -> None:
    """Import the rule data on first use (decoupled from import order).

    ``advisory_rules`` references targets by name only, so importing it never
    constructs a contract model and cannot deadlock model definition.
    ``functools.cache`` makes this a run-once without a module-global flag.
    """
    from . import advisory_rules  # noqa: F401  (import for side-effect: register)


def rules_for(cls: type) -> list[AdvisoryRule]:
    """All rules bound to a class or any of its ancestors, de-duplicated by id."""
    _ensure_loaded()
    out: dict[str, AdvisoryRule] = {}
    for ancestor in cls.__mro__:
        for rule in _RULES_BY_TARGET.get(ancestor.__name__, ()):
            out.setdefault(rule.id, rule)
    return list(out.values())


def all_rules() -> list[AdvisoryRule]:
    """The whole registry (for the JSON export, checklist, and doc generation)."""
    _ensure_loaded()
    return list(_ALL_RULES)


class AdvisoryValidated:
    """Mixin: run this model's registered advisory rules after construction.

    Inheriting classes get relational enforcement for free; a class with no
    registered rules pays only one dict lookup. Bespoke ``@model_validator``s on
    the same class continue to run — they own the ``custom`` rules the registry
    only catalogues.
    """

    @model_validator(mode="after")
    def _run_advisory_rules(self):
        for rule in rules_for(type(self)):
            check_rule(rule, self)
        return self
