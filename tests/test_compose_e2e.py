"""compose() end to end against a fake OpenAI-compatible gateway.

This is the test that was missing when `_default_generate` was lost in the src/
restructure and every real `compose` raised NameError while CI stayed green: it
drives the DEFAULT generation path (HTTP -> generator -> emitter), not an
injected callable.

Covers: reuse create (dry-run / --apply / overwrite rules), NL intent flow,
net-new refusal + --freeform, mutations (deterministic scalar set, LLM edit-set
'add', scope guard, no-op refusal, missing-component refusal), --regen,
write-time safety checks, CLI exit codes, and the tf-modules / tf-flat emitters.
"""
import json
import os
import tempfile

import harness
from harness import check, copy_fixture, read, captured, finish, point_at
from fake_gateway import FakeGateway

from terra_pilot.pipeline import compose
from terra_pilot.core.convention import detect_convention

GEN_TEXT = ('inputs = {\n  ami_id = "ami-0c55b159cbfafe1f0"\n  instance_type = "t3.small"\n'
            '  subnet_id = "subnet-0auth002"\n  vpc_id = "vpc-0auth002"\n'
            '  security_group_ids = ["sg-0auth002"]\n}\n')


def _gateway(intent=None, edits=None, gen=GEN_TEXT):
    def script(kind, messages):
        if kind == "intent":
            return intent
        if kind == "edit":
            return {"edits": edits or []}
        return gen if not callable(gen) else gen(messages)
    gw = FakeGateway(script).start()
    point_at(gw)
    return gw


# --------------------------------------------------------------------------- #
def test_reuse_create_default_generation_path():
    print("\n# reuse create through the real generator (HTTP)")
    repo = copy_fixture("myrepo")
    gw = _gateway(gen="```hcl\n" + GEN_TEXT + "```")
    try:
        a = compose.compose(repo, resource_type="ec2", project="auth", env="prod",
                            reference={"project": "auth", "env": "nonprod", "component": "ec2"})
    finally:
        gw.stop()
    gens = gw.chat_calls("generate")
    check("exactly one generation request", len(gens) == 1, str(len(gens)))
    check("bearer key sent to the gateway", gw.requests[-1].get("auth") == "Bearer test-key")
    user = gens[0]["body"]["messages"][1]["content"] if gens else ""
    check("prompt carries the module input contract", "REQUIRED INPUTS" in user and "ami_id" in user)
    check("prompt carries the reference inputs.hcl", "REFERENCE inputs.hcl" in user)
    check("prompt names the destination env-tier", "env-tier    = auth_prod" in user)
    check("decision is reuse", a.decision == "reuse", a.decision)
    check("code fence stripped, inputs = { ... }", a.inputs_hcl.startswith("inputs = {")
          and "```" not in a.inputs_hcl)
    check("standard terragrunt.hcl emitted", 'find_in_parent_folders("root.hcl")' in a.terragrunt_hcl)
    check("not refused", a.refused is False)
    target = os.path.join(repo, "auth", "aws", "auth_prod", "ec2")
    check("component dir resolved", os.path.normpath(a.component_dir) == os.path.normpath(target))
    check("dry-run wrote nothing", not os.path.exists(target))

    written = compose.write_to_tree(a)
    check("--apply wrote terragrunt.hcl + inputs.hcl",
          sorted(os.path.basename(w) for w in written) == ["inputs.hcl", "terragrunt.hcl"], str(written))
    check("inputs.hcl content on disk", read(os.path.join(target, "inputs.hcl")) == a.inputs_hcl)
    try:
        compose.write_to_tree(a)
        check("second apply refuses to overwrite", False)
    except FileExistsError:
        check("second apply refuses to overwrite", True)
    compose.write_to_tree(a, overwrite=True)
    check("overwrite=True replaces it", read(os.path.join(target, "inputs.hcl")) == a.inputs_hcl)


def test_thinking_option_is_forwarded_only_when_set():
    print("\n# LLM_THINKING -> {thinking: {type: ...}} in the request")
    from terra_pilot.core import config
    from terra_pilot.llm import generator
    gw = _gateway()
    saved = config.THINKING
    try:
        config.THINKING = ""
        generator.complete([{"role": "user", "content": "x"}])
        config.THINKING = "disabled"
        generator.complete([{"role": "user", "content": "x"}])
    finally:
        config.THINKING = saved
        gw.stop()
    a, b = [r["body"] for r in gw.chat_calls()]
    check("unset: no thinking field sent", "thinking" not in a)
    check("disabled: forwarded", b.get("thinking") == {"type": "disabled"})


def test_nl_intent_flow_and_cli_apply():
    print("\n# natural language -> intent -> compose -> run_cli --apply")
    repo = copy_fixture("myrepo")
    intent = {"resource_type": "ec2", "project": "auth", "env": "prod", "name": None,
              "region": None, "specifics": {}, "operation": "create", "notes": None,
              "reference": {"project": "auth", "env": "nonprod", "component": "ec2", "provider": None}}
    gw = _gateway(intent=intent)
    try:
        with captured() as out:
            rc = compose.run_cli(repo, ["deploy", "an", "ec2", "for", "auth", "prod",
                                        "like", "auth", "nonprod", "--apply"])
    finally:
        gw.stop()
    text = out.getvalue()
    check("exit code 0", rc == 0, text[-300:])
    check("one intent call + one generation call",
          len(gw.chat_calls("intent")) == 1 and len(gw.chat_calls("generate")) == 1)
    check("intent echoed", "# intent:" in text)
    check("files written", os.path.exists(os.path.join(repo, "auth", "aws", "auth_prod", "ec2", "inputs.hcl")))
    check("WROTE section printed", "# WROTE (ec2)" in text)

    # intent missing the destination env is a usage error, not a guess
    bad = dict(intent, env=None, project=None)
    gw = _gateway(intent=bad)
    try:
        with captured() as out:
            rc = compose.run_cli(repo, ["make", "an", "ec2", "like", "auth", "nonprod"])
    finally:
        gw.stop()
    check("missing destination -> exit 1", rc == 1)
    check("error names the missing fields", "project, env" in out.getvalue())


def test_net_new_is_refused_with_scaffold():
    print("\n# no module for the component -> refuse + scaffold (no LLM call)")
    repo = copy_fixture("payments")
    gw = _gateway()
    try:
        a = compose.compose(repo, resource_type="rds", project="payments", env="nonprod")
        calls_default = len(gw.chat_calls())
        with captured() as out:
            rc = compose.run_cli(repo, ["--resource-type", "rds", "--project", "payments",
                                        "--env", "nonprod", "--apply"])
        b = compose.compose(repo, resource_type="rds", project="payments", env="nonprod",
                            allow_freeform=True)
    finally:
        gw.stop()
    check("decision net-new", a.decision == "net-new")
    check("refused", a.refused is True)
    check("no module adopted (was: ec2 schema leaked into rds)", a.module_key is None)
    check("no LLM generation was attempted", calls_default == 0, str(calls_default))
    check("scaffold present and names aws_db_instance", "aws_db_instance" in a.scaffold)
    check("inputs stub is an explicit TODO, not invented fields",
          "TODO" in a.inputs_hcl and "ami_id" not in a.inputs_hcl)
    try:
        compose.write_to_tree(a)
        check("write_to_tree refuses a net-new stub", False)
    except ValueError as e:
        check("write_to_tree refuses a net-new stub", "refusing" in str(e))
    check("CLI --apply exits 1 and writes nothing",
          rc == 1 and not os.path.exists(a.component_dir), out.getvalue()[-200:])
    check("CLI printed the scaffold", "net-new module scaffold" in out.getvalue())
    check("--freeform generates (opt-in)", not b.refused and len(gw.chat_calls("generate")) == 1)
    check("--freeform warns loudly", any("--freeform" in n and "unverified" in n for n in b.notes))


def test_mutation_scalar_is_deterministic():
    print("\n# update: declared top-level scalar is set without any LLM call")
    repo = copy_fixture("myrepo")
    gw = _gateway()
    try:
        a = compose.compose(repo, resource_type="ec2", project="billing", env="nonprod",
                            operation="update", specifics={"instance_type": "t3.xlarge"})
    finally:
        gw.stop()
    check("no gateway traffic at all", len(gw.chat_calls()) == 0, str(gw.paths()))
    check("value changed", 'instance_type = "t3.xlarge"' in a.inputs_hcl.replace("  ", " ")
          or 't3.xlarge' in a.inputs_hcl)
    check("old value gone", "t3.large" not in a.inputs_hcl)
    check("everything else preserved byte-for-byte",
          'vpc_id        = "vpc-0billing001"' in a.inputs_hcl and 'cidr              = "10.170.0.0/22"' in a.inputs_hcl)
    check("change_applied True", a.change_applied is True)
    ip = a.inputs_file_path
    compose.write_to_tree(a, overwrite=True)
    check("mutation written in place", "t3.xlarge" in read(ip))

    # requesting the value that is already there is a no-op -> refuse to "succeed"
    repo2 = copy_fixture("myrepo")
    gw = _gateway(gen="inputs = {\n  instance_type = \"t3.large\"\n}\n")
    try:
        n = compose.compose(repo2, resource_type="ec2", project="billing", env="nonprod",
                            operation="update", specifics={"instance_type": "t3.large"})
    finally:
        gw.stop()
    check("no-op mutation flagged CHANGE NOT APPLIED", n.change_applied is False
          and any("CHANGE NOT APPLIED" in x for x in n.notes))
    try:
        compose.write_to_tree(n, overwrite=True)
        check("no-op mutation is not written", False)
    except ValueError as e:
        check("no-op mutation is not written", "did not apply" in str(e))


def test_mutation_add_via_llm_edit_set():
    print("\n# add: LLM edit-planner -> typed hcl_edit splice, scope-guarded")
    repo = copy_fixture("myrepo")
    element = {"name": "app-1c", "cidr": "10.170.8.0/22", "availability_zone": "ap-south-1c"}
    edits = [
        {"path": "workload_subnets", "op": "add", "value": element},
        {"path": "instance_type", "op": "set", "value": "t3.nano"},      # must be dropped
    ]
    gw = _gateway(edits=edits)
    try:
        a = compose.compose(repo, resource_type="network", project="billing", env="nonprod",
                            operation="add",
                            specifics={"cidr": "10.170.8.0/22", "availability_zone": "ap-south-1c",
                                       "name": "app-1c"})
    finally:
        gw.stop()
    check("edit-planner called once, no whole-file generation",
          len(gw.chat_calls("edit")) == 1 and len(gw.chat_calls("generate")) == 0)
    check("new element appended", "10.170.8.0/22" in a.inputs_hcl and "app-1c" in a.inputs_hcl)
    check("existing element preserved", "10.170.0.0/22" in a.inputs_hcl and "app-1a" in a.inputs_hcl)
    check("scope guard dropped the non-append edit", any("scope guard" in n for n in a.notes))
    check("change verified", a.change_applied is True)
    compose.write_to_tree(a, overwrite=True)
    disk = read(os.path.join(repo, "billing", "aws", "billing_nonprod", "network", "inputs.hcl"))
    check("splice landed on disk", "app-1c" in disk and disk.count("availability_zone") == 2)

    # 'add' with an edit-planner that only returns sets -> refuse, don't clobber
    repo2 = copy_fixture("myrepo")
    gw = _gateway(edits=[{"path": "vpc_cidr", "op": "set", "value": "10.0.0.0/8"}])
    try:
        b = compose.compose(repo2, resource_type="network", project="billing", env="nonprod",
                            operation="add", specifics={"cidr": "10.170.8.0/22"})
    finally:
        gw.stop()
    check("add with no append edit falls back and is not silently applied",
          "10.0.0.0/8" not in b.inputs_hcl)


def test_regen_runs_the_llm_after_a_verified_splice():
    print("\n# --regen: re-run whole-file generation after a verified edit")
    repo = copy_fixture("myrepo")
    restyled = 'inputs = {\n  instance_type = "t3.xlarge"\n  ami_id = "ami-restyled"\n}\n'
    gw = _gateway(gen=restyled)
    try:
        a = compose.compose(repo, resource_type="ec2", project="billing", env="nonprod",
                            operation="update", specifics={"instance_type": "t3.xlarge"}, regen=True)
        plain = compose.compose(repo, resource_type="ec2", project="billing", env="nonprod",
                                operation="update", specifics={"instance_type": "t3.xlarge"})
    finally:
        gw.stop()
    check("regen issued one generation request", len(gw.chat_calls("generate")) == 1)
    check("regen output used", "ami-restyled" in a.inputs_hcl)
    check("regen prompt carries the spliced current file",
          "CURRENT inputs.hcl" in gw.chat_calls("generate")[0]["body"]["messages"][1]["content"])
    check("without --regen the splice is kept verbatim", "ami-restyled" not in plain.inputs_hcl
          and "ami-0c55b159cbfafe1f0" in plain.inputs_hcl)


def test_mutation_of_missing_component_is_refused():
    print("\n# mutation verbs never create")
    repo = copy_fixture("myrepo")
    gw = _gateway()
    try:
        a = compose.compose(repo, resource_type="ec2", project="auth", env="prod",
                            operation="update", specifics={"instance_type": "t3.xlarge"})
    finally:
        gw.stop()
    check("refused, decision 'refused'", a.refused and a.decision == "refused")
    check("no generation attempted", len(gw.chat_calls()) == 0)
    try:
        compose.write_to_tree(a)
        check("refused compose cannot be written", False)
    except ValueError:
        check("refused compose cannot be written", True)


def test_editing_existing_component_without_module_is_not_refused():
    print("\n# net-new guard applies to creation only")
    repo = copy_fixture("myrepo")
    import shutil
    shutil.rmtree(os.path.join(repo, "modules", "aws", "infrastructure", "env", "ec2"))
    gw = _gateway(edits=[{"path": "instance_type", "op": "set", "value": "t3.xlarge"}])
    try:
        a = compose.compose(repo, resource_type="ec2", project="billing", env="nonprod",
                            operation="update", specifics={"instance_type": "t3.xlarge"})
    finally:
        gw.stop()
    check("existing component is still editable (no contract -> LLM edit-planner)",
          not a.refused and "t3.xlarge" in a.inputs_hcl and a.change_applied is True, str(a.notes))
    check("no misleading --freeform warning on an edit", not any("--freeform" in n for n in a.notes))


def test_write_time_safety_checks():
    print("\n# write_to_tree fails closed on broken output")
    repo = copy_fixture("myrepo")
    gw = _gateway(gen='inputs = {\n  ami_id = "ami-1"\n')            # truncated: missing '}'
    try:
        trunc = compose.compose(repo, resource_type="ec2", project="auth", env="prod")
    finally:
        gw.stop()
    check("truncation noted at compose time", any("brace balance" in n for n in trunc.notes))
    try:
        compose.write_to_tree(trunc)
        check("truncated file refused", False)
    except ValueError as e:
        check("truncated file refused", "brace balance" in str(e))
    check("nothing written for the refused file",
          not os.path.exists(os.path.join(repo, "auth", "aws", "auth_prod", "ec2", "inputs.hcl")))

    # balanced braces but a broken string: only the sanity tier catches this
    gw = _gateway(gen='inputs = {\n  ami_id = "ami-1\n}\n')
    try:
        bad = compose.compose(repo, resource_type="ec2", project="auth", env="prod")
    finally:
        gw.stop()
    try:
        compose.write_to_tree(bad)
        check("unterminated string refused by the HCL sanity tier", False)
    except ValueError as e:
        check("unterminated string refused by the HCL sanity tier", "sanity" in str(e), str(e))

    gw = _gateway(gen='inputs = {\n  ami_id = "TODO"\n  subnet_id = ""   # TODO\n}\n')
    try:
        todo = compose.compose(repo, resource_type="ec2", project="auth", env="prod")
    finally:
        gw.stop()
    check("TODO placeholders warn (policy: warn, not block)",
          any("TODO placeholder" in n for n in todo.notes))


def test_plan_only_needs_no_llm():
    print("\n# --plan-only")
    repo = copy_fixture("myrepo")
    gw = _gateway()
    try:
        with captured() as out:
            rc = compose.run_cli(repo, ["--resource-type", "ec2", "--project", "auth",
                                        "--env", "prod", "--plan-only"])
    finally:
        gw.stop()
    check("exit 0, decision reuse", rc == 0 and "decision      : reuse" in out.getvalue())
    check("zero gateway calls", len(gw.chat_calls()) == 0)


# --------------------------------------------------------------------------- #
def _mk(files):
    d = tempfile.mkdtemp(prefix="tp_tf_")
    for rel, text in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
    return d


def test_tf_modules_emitter_live():
    print("\n# tf-modules emitter through the gateway")
    repo = _mk({
        "modules/ec2/main.tf": 'resource "aws_instance" "this" {\n  ami = var.ami_id\n  instance_type = var.instance_type\n}\n',
        "modules/ec2/variables.tf": 'variable "ami_id" { type = string }\nvariable "instance_type" { type = string }\n',
        "envs/dev/main.tf": 'module "web" {\n  source = "../../modules/ec2"\n  ami_id = "ami-1"\n  instance_type = "t3.micro"\n}\n',
    })
    check("convention detected as tf-modules", detect_convention(repo).kind == "tf-modules")
    body = 'module "api" {\n  source        = "../../modules/ec2"\n  ami_id        = "ami-2"\n  instance_type = "t3.small"\n}\n'
    gw = _gateway(gen="```hcl\n" + body + "```")
    try:
        a = compose.compose(repo, resource_type="ec2", project="app", env="dev")
    finally:
        gw.stop()
    sysmsg = gw.chat_calls("generate")[0]["body"]["messages"][0]["content"] if gw.chat_calls("generate") else ""
    check("plain-terraform system prompt used", "module { ... }" in sysmsg or "`module" in sysmsg)
    check("module block returned, fence stripped", a.inputs_hcl.startswith('module "api"')
          and "```" not in a.inputs_hcl)
    check("no terragrunt.hcl for plain terraform", a.terragrunt_hcl == "")
    check("module contract reached the prompt",
          "ami_id" in gw.chat_calls("generate")[0]["body"]["messages"][1]["content"])
    written = compose.write_to_tree(a)
    check("placed beside the existing envs/dev/main.tf as a new file (never clobbers it)",
          written == [os.path.join(repo, "envs", "dev", "ec2.tf")], str(written))
    check("existing main.tf untouched", 'module "web"' in read(os.path.join(repo, "envs", "dev", "main.tf")))
    check("content on disk", read(written[0]) == a.inputs_hcl)


def test_plain_tf_placement_rules():
    print("\n# plain-Terraform file placement follows the repo's layout")
    from terra_pilot.core import paths
    mod = {"modules/ec2/main.tf": 'resource "aws_instance" "t" {}\n'}
    r = _mk(dict(mod, **{"environments/prod/vpc/main.tf": "# x\n"}))
    check("env dir with component sub-dirs -> <env>/<component>/main.tf",
          paths.plain_tf_target(r, "tf-modules", "app", "prod", "ec2")[1]
          == os.path.join(r, "environments", "prod", "ec2", "main.tf"))
    check("existing component dir is reused",
          paths.plain_tf_target(r, "tf-modules", "app", "prod", "vpc")[1]
          == os.path.join(r, "environments", "prod", "vpc", "main.tf"))
    r = _mk(dict(mod, **{"live/stage/main.tf": "# x\n"}))
    check("`live/` is recognised as an env root",
          paths.plain_tf_target(r, "tf-modules", "app", "stage", "ec2")[1]
          == os.path.join(r, "live", "stage", "ec2.tf"))
    check("unknown env in a module repo -> new <env>/<component>/main.tf",
          paths.plain_tf_target(r, "tf-modules", "app", "qa", "ec2")[1]
          == os.path.join(r, "qa", "ec2", "main.tf"))
    check("unknown env in a flat repo -> <repo>/<component>.tf",
          paths.plain_tf_target(r, "tf-flat", "app", "qa", "s3")[1] == os.path.join(r, "s3.tf"))


def test_tf_flat_emitter_live():
    print("\n# tf-flat emitter through the gateway")
    repo = _mk({
        "main.tf": 'resource "aws_s3_bucket" "logs" {\n  bucket = "logs"\n}\n',
        "variables.tf": 'variable "region" { type = string }\n',
    })
    check("convention detected as tf-flat", detect_convention(repo).kind == "tf-flat")
    body = 'resource "aws_s3_bucket" "assets" {\n  bucket = "assets"\n}\n'
    gw = _gateway(gen=body)
    try:
        a = compose.compose(repo, resource_type="s3", project="app", env="dev")
    finally:
        gw.stop()
    sysmsg = gw.chat_calls("generate")[0]["body"]["messages"][0]["content"] if gw.chat_calls("generate") else ""
    check("flat-terraform system prompt used", "resource configuration" in sysmsg)
    check("resource block returned", a.inputs_hcl.startswith('resource "aws_s3_bucket"'))
    check("no terragrunt.hcl for flat terraform", a.terragrunt_hcl == "")
    written = compose.write_to_tree(a)
    check("flat repo: new file at the repo root, not a made-up dev/s3/ dir",
          written == [os.path.join(repo, "s3.tf")], str(written))
    check("existing main.tf untouched", read(os.path.join(repo, "main.tf")).startswith('resource "aws_s3_bucket" "logs"'))
    check("content on disk", read(written[0]) == a.inputs_hcl)


if __name__ == "__main__":
    for fn in [test_reuse_create_default_generation_path, test_thinking_option_is_forwarded_only_when_set,
               test_nl_intent_flow_and_cli_apply,
               test_net_new_is_refused_with_scaffold, test_mutation_scalar_is_deterministic,
               test_mutation_add_via_llm_edit_set, test_regen_runs_the_llm_after_a_verified_splice,
               test_mutation_of_missing_component_is_refused, test_editing_existing_component_without_module_is_not_refused,
               test_write_time_safety_checks,
               test_plan_only_needs_no_llm, test_tf_modules_emitter_live, test_plain_tf_placement_rules, test_tf_flat_emitter_live]:
        fn()
    finish()
