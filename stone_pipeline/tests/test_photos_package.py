"""The photo process is application code in stone_pipeline/photos/ (review wave C10) and the glue's library code is
stone_pipeline/io (wave C10b); deploy/ keeps only thin entry points: the input fetch and deliverables upload CLIs (an
operator's manual entry, DEPLOY.md) and the overnight photos trigger. Pinned here: what deploy/ holds, that no
stone_pipeline module imports deploy, that each CLI only calls its io function, that the photo code names no source and
no product (the owner's rule: a per-source switch, product-agnostic in shared code), and that every entry point names
a module that exists."""
from __future__ import annotations

import ast
import importlib.util
import io
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHOTOS = ROOT / "stone_pipeline" / "photos"
GLUE = {"__init__", "fetch_inputs", "upload_artifacts", "start_photos_run"}
# The thin CLIs and the io function each calls (wave C10b): the library code is stone_pipeline/io.
CLIS = {"fetch_inputs": ("medusa_inputs", "fetch_all"), "upload_artifacts": ("deliverables", "upload_all")}


def _modules(folder):
    return sorted(p for p in folder.rglob("*.py") if "tests" not in p.parts)


def _deploy_imports(path):
    """The deploy modules a file imports (any `import deploy.x` / `from deploy import x` / `from deploy.x import y`)."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.split(".")[0] == "deploy"}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "deploy":
            found |= ({f"deploy.{a.name}" for a in node.names} if node.module == "deploy" else {node.module})
    return found


def _module_name(path):
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def test_deploy_holds_only_the_glue():
    assert {p.stem for p in (ROOT / "deploy").glob("*.py")} == GLUE


def test_the_photo_process_is_the_photos_package():
    assert {p.stem for p in PHOTOS.glob("*.py")} == {
        "__init__", "process", "select", "publish", "decisions", "review", "batch", "source", "close", "retouch_job",
        "diag", "edit_test", "mark_logo_step"}


def test_no_stone_pipeline_module_imports_deploy():
    edges = {(_module_name(p), target) for p in _modules(ROOT / "stone_pipeline") for target in _deploy_imports(p)}
    assert edges == set()


@pytest.mark.parametrize("cli", sorted(CLIS))
def test_a_deploy_cli_only_calls_its_io_function(cli):
    # wave C10b: an operator's entry point holds no logic and builds no AWS client of its own
    module, function = CLIS[cli]
    tree = ast.parse((ROOT / "deploy" / f"{cli}.py").read_text())
    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    assert [d.name for d in defs] == ["main"]
    calls = {ast.unparse(n.func) for n in ast.walk(defs[0]) if isinstance(n, ast.Call)}
    assert calls == {f"{module}.{function}"}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert not imported & {"boto3", "aws_client"} and module in imported


def test_the_glue_never_imports_the_photo_process():
    assert not {(p.stem, t) for p in (ROOT / "deploy").glob("*.py") for t in _deploy_imports(p) if t.split(".")[1]
                not in GLUE}


def _code_words(path):
    """The lower-cased text of a file's identifiers and string literals, docstrings and comments left out (prose may
    say what the code never does)."""
    src = path.read_text()
    tree = ast.parse(src)
    docstrings = {(n.body[0].lineno, n.body[0].col_offset) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
                  and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
                  and isinstance(n.body[0].value.value, str)}
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.NAME or (tok.type == tokenize.STRING and tok.start not in docstrings):
            out.append(tok.string.lower().replace("_", " "))
    return out


def _forbidden():
    """{word: why}: every source's name and code (the sources list), every product type and colour of the attribute
    vocabulary. The finish vocabulary is left out: its values are ordinary words the code uses for itself ("raw", the
    judge's answer text)."""
    from stone_pipeline.config.sources import load_sources
    from stone_pipeline.reference.loaders import load_attributes
    attrs = load_attributes()
    words = {name.lower(): f"source {name}" for name in load_sources()}
    words |= {cfg.source_code.lower(): f"source code of {name}" for name, cfg in load_sources().items() if cfg.source_code}
    for category in ("type", "color"):
        words |= {value.lower(): f"{category} {value}" for value in attrs.canonical_names(category)}
    return words


def test_the_photo_code_names_no_source_and_no_product():
    forbidden = _forbidden()
    assert len(forbidden) > 20                                           # the vocabularies were read, not empty
    found = {}
    for path in _modules(PHOTOS):
        for text in _code_words(path):
            for word, why in forbidden.items():
                if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text):
                    found.setdefault(f"{path.name}: {why}", text)
    assert found == {}


@pytest.mark.parametrize("script", ["deploy/run_pipeline.sh", "deploy/build_standardise_image.sh"])
def test_every_entry_point_a_script_names_is_a_module_that_exists(script):
    text = (ROOT / script).read_text()
    named = set(re.findall(r"python -m ([a-z_.]+)", text)) | set(re.findall(r"'python -m ([a-z_\\.]+)", text))
    assert named and "deploy.standardise" not in text
    for module in {m.replace("\\", "") for m in named}:
        if module.endswith("."):                                         # the build script's pattern of the run modes
            assert module == "stone_pipeline.photos."
            continue
        assert importlib.util.find_spec(module) is not None, module


def test_the_runner_starts_the_photos_run_by_its_module():
    from stone_pipeline.config.control import runner
    command = runner._build_command({"stage": "photos"})
    assert command[-2:] == ["-m", "stone_pipeline.photos.process"]
    assert importlib.util.find_spec(command[-1]) is not None
