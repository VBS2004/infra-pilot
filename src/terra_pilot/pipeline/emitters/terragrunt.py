import os
import json
from typing import Dict, List, Optional
from .base import EmitterStrategy

def _read_prompt(name: str) -> str:
    p = os.path.join(os.path.dirname(__file__), "prompts", name)
    return open(p, encoding="utf-8").read()

class TerragruntEmitter(EmitterStrategy):
    def __init__(self):
        self.sys_prompt = _read_prompt("terragrunt_sys.txt")
        self.edit_prompt = _read_prompt("edit_terragrunt.txt")
        
    def build_llm_prompt(self, resource_type: str, specifics: Dict[str, object], grounding: str,
                         required: List[str], optional: List[str],
                         decision: str = "reuse", existing: bool = False,
                         existing_inputs: Optional[str] = None,
                         reference_inputs: Optional[str] = None,
                         reference_label: Optional[str] = None,
                         dest_ctx: Optional[Dict[str, object]] = None,
                         source_ctx: Optional[Dict[str, object]] = None,
                         repo: Optional[str] = None) -> List[Dict[str, str]]:
        if existing and existing_inputs:
            verb = (f"This env-tier `{resource_type}` component ALREADY EXISTS. Start "
                    f"from its CURRENT inputs.hcl (below) and return the COMPLETE updated "
                    f"file, applying ONLY the requested change(s). Preserve every existing "
                    f"entry - names, roles, policies, values - verbatim unless the request "
                    f"explicitly changes it. Never drop/rename entries or swap real values "
                    f"for placeholders.")
        elif reference_inputs:
            verb = (f"Compose the `inputs.hcl` for a new env-tier `{resource_type}` "
                    f"component, reusing the existing `{resource_type}` module and MODELED "
                    f"ON the REFERENCE inputs.hcl provided below. Reproduce the reference's "
                    f"structure and real values; change ONLY env-specific identifiers "
                    f"(names embedding the source env, account IDs, regions) to fit the "
                    f"destination. Do NOT collapse or invent placeholder "
                    f"services/containers/buckets.")
        elif existing:
            verb = (f"Compose the `inputs.hcl` for the EXISTING env-tier "
                    f"`{resource_type}` component (fill the module's inputs).")
        elif decision == "net-new":
            verb = (f"Create a net-new env-tier `{resource_type}` component "
                    f"(no reusable module exists yet).")
        else:
            verb = (f"Compose the `inputs.hcl` for a new env-tier `{resource_type}` "
                    f"component, reusing the existing `{resource_type}` module.")

        user: List[str] = ["REQUEST: " + verb]
        if specifics:
            user.append("REQUESTED SETTINGS (JSON): " + json.dumps(specifics))
        if required:
            user.append("REQUIRED INPUTS: " + ", ".join(required))
        if optional:
            user.append("OPTIONAL INPUTS (omit unless requested): " + ", ".join(optional))
            
        schema_ctx = ""
        if repo:
            try:
                from terra_pilot.models import root_schema
                schema_ctx = root_schema.get_schema(repo).as_prompt_context() + "\n\n"
            except Exception:
                pass
                
        user.append("\nGROUNDING:\n" + schema_ctx + grounding)
        if existing and existing_inputs:
            user.append("\nCURRENT inputs.hcl (reproduce unless changed):\n" + existing_inputs)
        if reference_inputs and not (existing and existing_inputs):
            label = reference_label or "source to model on"
            user.append(f"\nREFERENCE inputs.hcl ({label}) -- reproduce its structure and "
                        f"real values, changing ONLY the env-specific identifiers to match "
                        f"the destination:\n" + reference_inputs)
            if dest_ctx:
                dl = ["\nDESTINATION -- rename the source env's identifiers to THESE. "
                      "NEVER use generic placeholders like \"newenv\", \"sample-*\", or "
                      "\"dev-ecs\":"]
                dl.append(f"  project     = {dest_ctx.get('project')}")
                dl.append(f"  env-tier    = {dest_ctx.get('env_tier')}   # use as env_name "
                          f"and as the prefix in identifiers that embed the env "
                          f"(cluster_name, bucket_name, <env>-ecs-cluster, role names, etc.)")
                if dest_ctx.get("environment"):
                    dl.append(f"  environment = {dest_ctx.get('environment')}")
                if dest_ctx.get("region"):
                    dl.append(f"  region      = {dest_ctx.get('region')}")
                if source_ctx:
                    env_toks = [t for t in (source_ctx.get("env_tier"),
                                            source_ctx.get("environment")) if t]
                    if env_toks:
                        dl.append("  SOURCE env tokens to replace with the destination "
                                  "env-tier above: "
                                  + ", ".join('"%s"' % t for t in env_toks))
                    if source_ctx.get("project") and dest_ctx.get("project"):
                        dl.append(f"  SOURCE project token to replace with "
                                  f"'{dest_ctx.get('project')}': \"{source_ctx.get('project')}\" "
                                  f"(in resource names, container names, ECR registry paths, ARNs).")
                acct = dest_ctx.get("account_id")
                if acct:
                    dl.append(f"  ECR image URIs & ARNs: rewrite the AWS account ID to \"{acct}\" "
                              f"and the project namespace as above; keep the image name and tag.")
                else:
                    dl.append("  ECR image URIs & ARNs: rewrite the project namespace as above; "
                              "LEAVE the AWS account ID as-is (unknown for the destination) -- "
                              "do NOT invent one; it is flagged for review.")
                dl.append("  Keep values you cannot derive (RESOURCE_IDs, KMS key IDs, secrets) "
                          "VERBATIM -- never invent them; they are reviewed before apply.")
                user.append("\n".join(dl))

        requested_name = (specifics or {}).get("name")
        if requested_name:
            user.append(
                f'\nNAME OVERRIDE: the requested resource name is "{requested_name}". '
                f"Set the module's primary name/identifier input -- the one declared in "
                f'the INPUT CONTRACT above -- to this value, overriding any existing or '
                f'convention-derived value. Apply this even when reproducing an existing file.')

        user.append("\nEmit inputs.hcl now.")
        return [{"role": "system", "content": self.sys_prompt},
                {"role": "user", "content": "\n".join(user)}]

    def finalize_generation(self, raw: str) -> str:
        t = raw.strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[1] if "\n" in t else ""
            if t.rstrip().endswith("```"):
                t = t.rstrip()[:-3]
        t = t.strip()
        if not __import__('re').match(r"^\s*inputs\s*=", t):
            if t.startswith("{") and t.endswith("}"):
                t = "inputs = " + t
            else:
                lines = t.splitlines()
                indented = "\n".join(("  " + ln if ln.strip() else ln) for ln in lines)
                t = "inputs = {\n" + indented + "\n}"
        return t if t.endswith("\n") else t + "\n"

    def render_module_call(self, module, project: str, env_tier: str, upstreams: List[str]) -> str:
        depth_prefix = "../../../../"
        source = depth_prefix + module.key

        lines = []
        lines.append('include "root" {')
        lines.append('  path = find_in_parent_folders()')
        lines.append('}')
        lines.append('')
        lines.append('terraform {')
        lines.append(f'  source = "{source}"')
        lines.append('}')
        lines.append('')
        for up in upstreams:
            lines.append(f'dependency "{up}" {{')
            lines.append(f'  config_path = "../{up}"')
            lines.append('}')
            lines.append('')
        if upstreams:
            lines.append('locals {')
            lines.append('  upstream_modules = {')
            for up in upstreams:
                lines.append(f'    {up} = "{up}"')
            lines.append('  }')
            lines.append('}')
            lines.append('')
        lines.append('inputs = {')
        for i in module.required_inputs:
            placeholder = self._placeholder(i.name, getattr(i, "type", ""), project, env_tier, upstreams)
            lines.append(f'  {i.name} = {placeholder}')
        wired_optional, commented_optional = [], []
        for i in module.optional_inputs:
            ph = self._placeholder(i.name, getattr(i, "type", ""), project, env_tier, upstreams)
            if 'module.remote_state' in ph or 'dependency.' in ph:
                wired_optional.append((i, ph))
            else:
                commented_optional.append(i)
        for i, ph in wired_optional:
            lines.append(f'  {i.name} = {ph}')
        if commented_optional:
            lines.append('')
            lines.append('  # optional (module defaults apply if omitted):')
            for i in commented_optional:
                t = getattr(i, "type", "")
                lines.append(f'  # {i.name} = ...   # {t}')
        lines.append('}')
        return "\n".join(lines) + "\n"

    def _placeholder(self, name: str, vtype: str, project: str, env_tier: str, upstreams: List[str]) -> str:
        n = name.lower()
        if name == "environment":
            return "local.env.locals.environment"
        if "security_group_ids" in n and upstreams:
            up = next((u for u in upstreams if "security" in u), upstreams[0])
            return (f'[module.remote_state.components[var.upstream_modules.{up}]'
                    f'["security_group_id"]]')
        if vtype.startswith("list"):
            return "[]   # TODO"
        if vtype.startswith("number"):
            return "0    # TODO"
        if vtype.startswith("bool"):
            return "false # TODO"
        return '""   # TODO'

    def scaffold_new_module(self, intent: str, project: str, nearest) -> str:
        rtype = self._guess_resource_type(intent)
        name = self._guess_module_name(intent)
        ref = f"  # style reference: {nearest.key}" if nearest else ""
        env_var = chr(36) + chr(123) + "var.environment" + chr(125)
        files = {
            f"modules/aws/resource/{name}/main.tf":
                (f'resource "{rtype}" "this" {{{ref}\n'
                 f'  # TODO: generator fills the body, grounded in retrieved siblings\n'
                 f'  tags = merge(local.default_tags, {{ Name = "{project}-{env_var}-{name}" }})\n'
                 f'}}\n\nlocals {{\n  default_tags = {{ Project = "{project}", Environment = var.environment }}\n}}\n'),
            f"modules/aws/resource/{name}/variables.tf":
                'variable "environment" {\n  type = string\n}\n# TODO: add the inputs this resource needs\n',
            f"modules/aws/resource/{name}/outputs.tf":
                'output "id" {\n  value = ' + rtype + '.this.id\n}\n',
        }
        out = [f"# NET-NEW module scaffold for: {intent}",
               f"# guessed resource type: {rtype}, module name: {name}", ""]
        for path, body in files.items():
            out.append(f"# ===== {path} =====")
            out.append(body)
        return "\n".join(out)

    def _guess_resource_type(self, intent: str) -> str:
        table = {
            "ec2": "aws_instance", "instance": "aws_instance",
            "security group": "aws_security_group", "sg": "aws_security_group",
            "s3": "aws_s3_bucket", "bucket": "aws_s3_bucket",
            "rds": "aws_db_instance", "database": "aws_db_instance",
            "alb": "aws_lb", "load balancer": "aws_lb",
            "lambda": "aws_lambda_function", "sqs": "aws_sqs_queue",
        }
        low = intent.lower()
        for k, v in table.items():
            if k in low:
                return v
        return "aws_RESOURCE"

    def _guess_module_name(self, intent: str) -> str:
        rt = self._guess_resource_type(intent)
        return rt.replace("aws_", "").replace("_instance", "").replace("db", "rds") \
            if rt != "aws_RESOURCE" else "new_resource"
