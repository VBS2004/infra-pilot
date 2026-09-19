"""The CLI commands that had no coverage: outline, find, imports, importers,
related, search, tool, list (explicit + natural language), livecheck, plus the
env-directory resolution that compose depends on."""
import json
import os

import harness
from harness import check, copy_fixture, captured, finish, point_at, FIXTURES
from fake_gateway import FakeGateway

from terra_pilot.cli import cli
from terra_pilot.core import paths, config

MY = os.path.join(FIXTURES, "myrepo")
PAY = os.path.join(FIXTURES, "payments")


def run(repo, *args):
    with captured() as out:
        rc = cli.main(["cli.py", repo, *args])
    return rc, out.getvalue()


def js(text):
    return json.loads(text)


def test_index_commands():
    print("\n# stats / catalog / outline / find")
    rc, out = run(MY, "stats")
    d = js(out)
    check("stats: exit 0, files + symbols counted", rc == 0 and d["files"] > 10 and d["symbols"] > 10)
    check("stats: symbol kinds include resource + variable",
          "resource" in d["symbol_kinds"] and "variable" in d["symbol_kinds"])
    rc, out = run(MY, "catalog")
    mods = {m["module"]: m for m in js(out)}
    ec2 = next(m for k, m in mods.items() if k.endswith("env/ec2"))
    check("catalog: ec2 module with aws_instance and required inputs",
          "aws_instance" in ec2["resources"] and "ami_id" in ec2["required"])
    rc, out = run(MY, "outline", "modules/aws/infrastructure/env/ec2/main.tf")
    names = [s["name"] for s in js(out)]
    check("outline: lists the file's resources", "aws_instance.this" in names, str(names))
    rc, out = run(MY, "find", "aws_instance")
    check("find: locates the aws_instance symbol",
          any(r["name"].startswith("aws_instance") for r in js(out)))
    rc, out = run(MY, "find", "aws_instance", "--kind", "variable")
    check("find --kind filters by kind", js(out) == [])
    rc, out = run(MY, "nonsense-command")
    check("unknown command -> usage, exit 1", rc == 1)


def test_graph_commands():
    print("\n# imports / importers / related on a repo with real module sources")
    rc, out = run(PAY, "imports", "live/nonprod/payments/ec2/terragrunt.hcl")
    edges = js(out)
    check("imports: terragrunt edges returned", rc == 0 and any(e["edge_type"] == "terragrunt" for e in edges))
    check("imports: source resolved to the module dir",
          any("modules/aws/resource/ec2" in (e.get("target_file") or e.get("module") or "")
              for e in edges), json.dumps(edges)[:300])
    rc, out = run(PAY, "importers", "modules/aws/resource/ec2")
    imps = js(out)
    check("importers: the consuming terragrunt.hcl found",
          any(i.endswith("live/nonprod/payments/ec2/terragrunt.hcl") for i in imps), str(imps))
    rc, out = run(PAY, "importers", "modules/aws/resource/does_not_exist")
    check("importers: unknown module -> empty list", js(out) == [])
    rc, out = run(PAY, "related", "modules/aws/resource/ec2/main.tf")
    rel = js(out)
    check("related: imports + importers sections", "imports" in rel and rel["file"].endswith("ec2/main.tf"))


def test_search_and_tool():
    print("\n# search / tool (BM25-only)")
    rc, out = run(MY, "search", "ec2", "instance")
    hits = [h for h in js(out) if "file" in h]
    check("search: returns ranked hits", rc == 0 and len(hits) > 0)
    check("search: top hit is about ec2", "ec2" in hits[0]["file"] or "ec2" in str(hits[0].get("symbol", "")))
    rc, out = run(MY, "tool", "ec2")
    check("tool: string output from tf_search", rc == 0 and isinstance(out, str) and len(out) > 20)
    rc, out = run(MY, "plan", "an", "ec2", "instance")
    check("plan: reuse decision + rendered module call",
          "# DECISION: reuse" in out and "inputs = {" in out)
    rc, out = run(MY, "plan", "a", "kinesis", "stream")
    check("plan: no module -> write_new + scaffold", "# DECISION: write_new" in out and "NET-NEW" in out)


def test_list_inventory():
    print("\n# list (deterministic inventory)")
    rc, out = run(MY, "list", "ec2", "--project", "billing")
    check("list explicit: finds billing's ec2",
          rc == 0 and "1 'ec2' component(s) in project 'billing'" in out and "billing_nonprod" in out, out)
    rc, out = run(MY, "list", "ec2")
    check("list (NL form): both projects", "auth_nonprod" in out and "billing_nonprod" in out, out)
    rc, out = run(MY, "list", "network", "--where", "enable_dns_hostnames=true")
    check("list --where filters on a top-level scalar",
          rc == 0 and "auth_nonprod" in out and "billing_nonprod" in out and "auth_prod" in out, out)
    rc, out = run(MY, "list", "ec2", "--where", "instance_type=t3.large")
    check("list --where narrows to one match", "1 'ec2' component(s)" in out and "billing_nonprod" in out, out)
    rc, out = run(MY, "list", "ec2", "--where", "instance_type!=t3.large")
    check("list --where != excludes matches", "auth_nonprod" in out and "billing_nonprod" not in out, out)
    rc, out = run(MY, "list", "ec2", "--where", "workload_subnets.name=app-1a")
    check("list warns when a predicate key is not a top-level scalar",
          "WARNING" in out and "workload_subnets.name" in out, out)
    rc, out = run(MY, "list", "eks", "--project", "nope")
    check("list: unknown project -> zero rows, exit 0", rc == 0 and "0 'eks' component(s)" in out)
    rc, out = run(MY, "list", "--project")
    check("list: dangling flag -> exit 1", rc == 1)
    rc, out = run(MY, "list", "--bogus", "x")
    check("list: unknown flag -> exit 1", rc == 1 and "unknown flag" in out)


def test_env_dir_resolution():
    print("\n# env-tier directory resolution (regression: auth_auth_prod)")
    base = paths.project_dir(MY, "auth")
    check("short word -> <project>_<word>", paths.env_dir(MY, "auth", "prod") == os.path.join(base, "auth_prod"))
    check("full dir name is not prefixed twice", paths.env_dir(MY, "auth", "auth_prod") == os.path.join(base, "auth_prod"))
    check("new env word still gets the project prefix",
          paths.env_dir(MY, "auth", "staging") == os.path.join(base, "auth_staging"))
    check("component dir uses it", paths.component_dir(MY, "auth", "auth_nonprod", "ec2")
          == os.path.join(base, "auth_nonprod", "ec2"))
    check("env config found via either spelling",
          paths.load_env_config(MY, "auth", "nonprod") == paths.load_env_config(MY, "auth", "auth_nonprod")
          and paths.load_env_config(MY, "auth", "nonprod") != {})


def test_livecheck():
    print("\n# livecheck against the fake gateway")
    from terra_pilot.utils import livecheck
    saved = (config.GATEWAY_BASE, config.API_KEY)
    config.GATEWAY_BASE, config.API_KEY = "http://127.0.0.1:9", ""
    with captured() as out:
        rc = livecheck.main(MY)
    check("unconfigured: exit 1 with instructions", rc == 1 and "Not configured" in out.getvalue())
    gw = FakeGateway(lambda kind, msgs: "ok").start()
    point_at(gw)
    try:
        with captured() as out:
            rc = livecheck.main(MY)
        text = out.getvalue()
    finally:
        gw.stop()
        config.GATEWAY_BASE, config.API_KEY = saved
    check("configured: exit 0", rc == 0, text[-300:])
    check("GET /v1/models probed", "GET /v1/models -> 200" in text)
    check("chat smoke test ok", "ok -> 'ok'" in text)
    check("embeddings reported disabled (offline env)", "disabled" in text)
    check("hybrid search ran", "== hybrid search ==" in text and "reranker wired: False" in text)
    check("gateway saw models + chat", any(p.endswith("/models") for p in gw.paths())
          and len(gw.chat_calls()) == 1)


if __name__ == "__main__":
    for fn in [test_index_commands, test_graph_commands, test_search_and_tool, test_list_inventory,
               test_env_dir_resolution, test_livecheck]:
        fn()
    finish()
