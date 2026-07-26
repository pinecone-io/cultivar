"""Per-run Pinecone index-name isolation for execution-based eval verify.

Execution-based grading runs the agent's generated code against a real, shared
Pinecone project. Many (task, variant, repeat) runs execute in parallel and the
generated scripts hard-code index names, so two runs of the same task would
collide on `create_index` (409) and a delete-all teardown would nuke a sibling
run's index mid-flight.

This module transparently prefixes every index name the generated code touches
(create / connect / describe / delete) with a per-run token read from
``EVAL_IDX_PREFIX`` (injected into the sandbox by modal_runner). ``teardown()``
then deletes only this run's prefixed indexes. Net effect: runs are isolated and
parallel-safe without rewriting the generated code.

Usage inside the sandbox verify step:
    PYTHONPATH=/workspace/evals/remote python3 -c \
      "import eval_isolate, runpy; eval_isolate.apply(); runpy.run_path('f.py', run_name='__main__')"
and in teardown:
    PYTHONPATH=/workspace/evals/remote python3 -c "import eval_isolate; eval_isolate.teardown()"
"""

import os

PREFIX = os.environ.get("EVAL_IDX_PREFIX", "")


def _pfx(v):
    if isinstance(v, str) and v and not v.startswith(PREFIX):
        return PREFIX + v
    return v


def _wrap(fn, name_pos):
    def w(*a, **k):
        if "name" in k:
            k["name"] = _pfx(k["name"])
        elif "index_name" in k:
            k["index_name"] = _pfx(k["index_name"])
        elif len(a) > name_pos and isinstance(a[name_pos], str):
            a = list(a)
            a[name_pos] = _pfx(a[name_pos])
            a = tuple(a)
        return fn(*a, **k)

    w._eval_wrapped = True
    return w


def _patch(cls, methods, name_pos=1):
    for m in methods:
        orig = getattr(cls, m, None)
        if callable(orig) and not getattr(orig, "_eval_wrapped", False):
            try:
                setattr(cls, m, _wrap(orig, name_pos))
            except Exception:
                pass


def apply():
    """Monkeypatch the Pinecone client so all index names get the run prefix."""
    if not PREFIX:
        return
    import pinecone

    P = pinecone.Pinecone
    # name is the first real positional arg; bound-method calls pass self at pos 0.
    _patch(
        P,
        ["create_index", "create_index_for_model", "Index", "delete_index", "describe_index", "has_index", "configure_index"],
        name_pos=1,
    )
    # Preview / documents-API namespace: introspect a live client to find the
    # concrete classes, then patch them. Best-effort — never fail the run.
    try:
        pc = P(api_key=os.environ["PINECONE_API_KEY"])
        prev = pc.preview
        _patch(type(prev), ["index", "create_index", "delete_index", "describe_index", "has_index"], name_pos=1)
        idxs = getattr(prev, "indexes", None)
        if idxs is not None:
            _patch(type(idxs), ["create", "delete", "describe", "get", "has"], name_pos=1)
    except Exception:
        pass


def teardown():
    """Delete only the indexes this run created (those carrying the run prefix)."""
    if not PREFIX:
        return
    try:
        import pinecone

        pc = pinecone.Pinecone(api_key=os.environ["PINECONE_API_KEY"])
        try:
            names = list(pc.list_indexes().names())
        except Exception:
            names = []
            for i in pc.list_indexes():
                n = getattr(i, "name", None) or (i.get("name") if isinstance(i, dict) else None)
                if n:
                    names.append(n)
        for n in names:
            if n and n.startswith(PREFIX):
                try:
                    pc.delete_index(n)
                    print("torn down", n)
                except Exception as e:
                    print("teardown skip", n, e)
    except Exception as e:
        print("teardown error", e)


if __name__ == "__main__":
    teardown()
