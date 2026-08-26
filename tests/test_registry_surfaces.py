"""The Registry's two callers have two contracts, and this asserts the difference.

`Registry` is one class with fourteen public methods and two callers that share
almost nothing. The listener drives it from an MLLP socket: every call carries a
`control_id`, because the thing that caused it was a message, and the message is
what the clinical watermark and the duplicate index are keyed on. The worklist
drives it from a browser: every call carries an `actor` and a `role`, because
the thing that caused it was a person clicking a button, and an audit record
that cannot name who acted is not an audit record -- it is a timestamp with a
verb attached.

`registry.acknowledge` already states the half of this that bites. A human
action must not advance the clinical watermark, because a coordinator clicking
acknowledge is not evidence that the sending system is alive; if it advanced the
watermark, an acknowledgement made today would make a later correction whose
MSH-7 predates it look stale, and safety rule 2 would quietly stop firing. That
argument has been written down in a docstring for as long as the method has
existed. A docstring cannot fail a build.

So this file is the build failing. The rule it enforces is deliberately
mechanical rather than tasteful, because the seam is mechanical: a method that
names an `actor` is a coordinator action and a method that does not is an ingest
action, and the two sets are disjoint. Nothing here judges whether a given
method *should* take an actor. It asserts only that the surface a method is
published on and the signature it actually has agree, which is the property that
silently breaks when somebody adds a fifteenth method in six months and puts it
wherever the cursor happened to be.

**Nothing below hardcodes a method list.** Both the class and the two protocols
are read out of `registry.py` with `ast`, and the signatures are read off the
real objects with `inspect`. A test that names the methods it expects is a test
that asserts the author typed the same list twice; it passes on the day it is
written and says nothing on any day after. The point of enumerating from source
is that a method added to `Registry` and to neither protocol fails test 1 by
existing -- the author does not have to remember this file for this file to
catch them.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from referral_loop.registry import CoordinatorActions, IngestActions, Registry

# The one read. `get` replays a loop and changes nothing, so it names neither a
# control_id nor an actor and belongs to the actor rule in neither direction: it
# is published on the coordinator surface because the worklist renders state
# after every action, not because reading is a human act. Kept as a set rather
# than a special case in each assertion so that a second read method -- if one
# is ever justified -- is admitted in one place, in the open, rather than by
# loosening a rule that exists to catch exactly this kind of addition.
READS = frozenset({"get"})

_REGISTRY_SOURCE = pathlib.Path(inspect.getfile(Registry))


def _classes_in_registry_module() -> dict[str, ast.ClassDef]:
    tree = ast.parse(_REGISTRY_SOURCE.read_text(encoding="utf-8"))
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


def _method_names(class_name: str) -> set[str]:
    """Public method names declared in `class_name`, read from the source file.

    From source rather than from `dir()` because `dir()` on a Protocol reports
    the machinery `typing` mixes in, and filtering that by name would reintroduce
    the hardcoded list this module refuses to keep.
    """
    cls = _classes_in_registry_module()[class_name]
    return {
        node.name
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }


def _named_parameters(func) -> set[str]:
    return set(inspect.signature(func).parameters)


def test_every_public_registry_method_is_on_exactly_one_surface():
    """A method on neither surface is unreachable through either caller's type.

    This is the assertion that does the work in a year's time. The two protocols
    are the published contracts; `Registry` is the implementation that satisfies
    both. A public method that appears on the class and on neither protocol has
    no declared caller, which means either it is dead -- and should be deleted
    rather than left as a loaded gun for the next person to find -- or somebody
    added it to the class and forgot the surface, and the listener or the
    worklist is about to call it through a type that does not admit it.
    """
    on_class = _method_names("Registry")
    published = _method_names("IngestActions") | _method_names("CoordinatorActions")

    unpublished = on_class - published
    assert not unpublished, (
        f"Public Registry methods on neither surface: {sorted(unpublished)}. "
        "Every public method must be declared on IngestActions or on "
        "CoordinatorActions, so that the caller's annotation names it and a type "
        "checker can tell an ingest call from a human one."
    )

    phantom = published - on_class
    assert not phantom, (
        f"Declared on a surface but absent from Registry: {sorted(phantom)}. "
        "A protocol method with no implementation behind it is a contract "
        "nothing satisfies; mypy would accept the call and the process would "
        "raise AttributeError at the socket."
    )


def test_the_two_surfaces_do_not_overlap():
    """One method on both surfaces would erase the distinction for that method.

    The protocols are not merely two views of a convenient grouping -- they are
    the mechanism by which a coordinator action is prevented from reaching an
    ingest code path. A method published on both is reachable from both, and for
    that method the watermark invariant is back to being a docstring.
    """
    both = _method_names("IngestActions") & _method_names("CoordinatorActions")
    assert not both, (
        f"On both surfaces: {sorted(both)}. If a method genuinely serves both "
        "callers it needs two entry points with two contracts, not one entry "
        "point published twice."
    )


def test_no_ingest_action_names_an_actor():
    """An actor on an ingest path is a fiction, and the watermark it moves is a lie.

    Nothing on the ingest side has a human in it. The caller is an MLLP socket
    holding a message an interface engine sent; there is no session, no login and
    nobody to name. An `actor` parameter here could only be filled with a
    constant or with the peer id, and either one puts a name in the audit trail
    for an action no person took -- which is worse than an anonymous record,
    because it is a record that answers "who acted" wrongly and confidently.

    The consequence is the one `acknowledge` spells out in reverse. Ingest calls
    carry `message_at` and advance the clinical watermark, and that is right:
    they *are* evidence that the sending system is alive and current. Give the
    ingest surface an actor and the two paths become interchangeable at the type
    level, and the next refactor routes a human action through one -- at which
    point the site's record of when it last heard from the RIS includes a moment
    when it heard from nobody at all.
    """
    offenders = {
        name: sorted(_named_parameters(getattr(Registry, name)) & {"actor", "role"})
        for name in _method_names("IngestActions")
        if _named_parameters(getattr(Registry, name)) & {"actor", "role"}
    }
    assert not offenders, (
        f"Ingest-surface methods naming a human: {offenders}. Ingest is driven by "
        "a message, not a person. If this method really is a human action, move "
        "it to CoordinatorActions; if it is not, it has no actor to name."
    )


def test_every_coordinator_action_names_an_actor():
    """An audited human action that cannot say who acted is not an audit record.

    Every method on this surface is something a coordinator did on purpose,
    through a browser, and each one either resolves a loop or reverses a
    resolution. Those are precisely the events an audit trail exists to answer
    for -- `acknowledge` already refuses at runtime when `actor` or `role` is
    empty, on the grounds that a resolution attributed to nobody cannot say who
    vouched for the match or on what authority.

    That refusal is a runtime check on a value. This is a build-time check on the
    signature, and it catches the case the runtime check cannot: a coordinator
    method that never asked for an actor in the first place, and so has nothing
    to refuse.

    Reads are exempt and only reads. `get` replays a loop and appends nothing, so
    there is no record for a name to be missing from.
    """
    missing = sorted(
        name
        for name in _method_names("CoordinatorActions") - READS
        if "actor" not in _named_parameters(getattr(Registry, name))
    )
    assert not missing, (
        f"Coordinator-surface methods with no actor: {missing}. Each of these "
        "writes an audit record for a human action; a record that cannot name "
        "who acted answers the only question it was written to answer with "
        "silence."
    )


def test_reads_are_not_published_as_ingest_actions():
    """The read is on the coordinator surface because that is who reads.

    Small, but it stops the exemption above from being borrowed. `READS` names
    the methods excused from the actor rule; if a read were also published to
    ingest, the exemption would be a hole in the surface the actor rule is
    protecting rather than a carve-out in the one it is applied to.
    """
    leaked = READS & _method_names("IngestActions")
    assert not leaked, (
        f"Read methods published to ingest: {sorted(leaked)}. The listener does "
        "not render state; if it needs to read a loop, that is a design change "
        "worth arguing rather than an import."
    )


def test_registry_satisfies_both_surfaces_structurally():
    """Structural, not nominal -- `Registry` must not inherit from either protocol.

    Inheriting would make the relationship a fact about the class hierarchy, and
    the class would then drag both contracts everywhere it goes: `isinstance`
    would answer yes to a coordinator surface inside the listener, and the
    seam would be documentation again. Structural conformance means the listener
    holds something that is *only* an ingest surface as far as its own module can
    tell, which is the entire mechanism.

    Asserted against `__mro__` rather than `issubclass`, because neither protocol
    is `@runtime_checkable` and so `issubclass` against one raises rather than
    answering. That refusal is itself the right default and worth keeping: an
    `isinstance` check against a surface would be a runtime test for a property
    that is supposed to be settled at build time, and code that asks it at
    runtime has already decided to branch on which caller it is serving -- which
    is the shape these protocols exist to make unnecessary. `__mro__` asks the
    narrower question directly: is the relationship nominal? It must not be.
    """
    assert IngestActions not in Registry.__mro__, (
        "Registry inherits IngestActions. The conformance must be structural; "
        "inheritance re-attaches the contract to the class rather than to the "
        "call site, which is where it needs to bind."
    )
    assert CoordinatorActions not in Registry.__mro__, (
        "Registry inherits CoordinatorActions; see IngestActions above."
    )

    for surface in ("IngestActions", "CoordinatorActions"):
        for name in _method_names(surface):
            assert hasattr(Registry, name), (
                f"{surface}.{name} has no implementation on Registry."
            )


def test_surface_signatures_match_the_implementation():
    """A protocol that has drifted from the class type-checks calls that cannot run.

    The protocols exist to be annotated at a call site, so their signatures are
    what mypy checks the call against -- not the real ones. A protocol method
    that still declares a parameter `Registry` has since renamed is a call mypy
    approves and Python rejects, and the failure surfaces at the socket rather
    than in the build. Checking equality here keeps the copy honest.

    `*args, **kwargs` is tolerated deliberately: a surface may reasonably decline
    to restate an unwieldy signature, and a protocol that says only "this method
    exists, called somehow" is still strictly more than the class said before.
    What is not tolerated is a signature that is spelled out and wrong.
    """
    mismatched = {}
    for surface_name, surface in (
        ("IngestActions", IngestActions),
        ("CoordinatorActions", CoordinatorActions),
    ):
        for name in _method_names(surface_name):
            declared = inspect.signature(getattr(surface, name))
            if any(
                p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
                for p in declared.parameters.values()
            ):
                continue
            actual = inspect.signature(getattr(Registry, name))
            if str(declared) != str(actual):
                mismatched[f"{surface_name}.{name}"] = {
                    "surface": str(declared),
                    "registry": str(actual),
                }

    assert not mismatched, (
        "Surface signatures have drifted from Registry: "
        f"{mismatched}. Restate the real signature, or fall back to "
        "(*args, **kwargs) if it is genuinely unwieldy -- but do not leave a "
        "wrong one, because mypy believes it."
    )
