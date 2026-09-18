import os
import json
from typing import Dict, List, Optional
from .base import EmitterStrategy

def _read_prompt(name: str) -> str:
    p = os.path.join(os.path.dirname(__file__), "prompts", name)
    return open(p, encoding="utf-8").read()

class FlatTFEmitter(EmitterStrategy):
    def __init__(self):
        self.sys_prompt = _read_prompt("flat_tf_sys.txt")
        self.edit_prompt = _read_prompt("edit_plain_tf.txt") # Can reuse plain TF edit prompt

    def build_llm_prompt(self, resource_type: str, specifics: Dict[str, object], grounding: str,
                         required: List[str], optional: List[str],
                         decision: str = "reuse", existing: bool = False,
                         existing_inputs: Optional[str] = None,
                         reference_inputs: Optional[str] = None,
                         reference_label: Optional[str] = None,
                         dest_ctx: Optional[Dict[str, object]] = None,
                         source_ctx: Optional[Dict[str, object]] = None,
                         repo: Optional[str] = None) -> List[Dict[str, str]]:
        verb = f"Compose the standard Terraform `resource` block for `{resource_type}`."
        if existing and existing_inputs:
            verb = "Update the existing configuration below with the requested changes."
        elif reference_inputs:
            verb += " Model it on the REFERENCE configuration below."

        user = ["REQUEST: " + verb]
        if specifics:
            user.append("REQUESTED SETTINGS (JSON): " + json.dumps(specifics))
        
        user.append("\nGROUNDING:\n" + grounding)
        if existing and existing_inputs:
            user.append("\nCURRENT CONFIGURATION:\n" + existing_inputs)
        if reference_inputs and not (existing and existing_inputs):
            user.append("\nREFERENCE CONFIGURATION:\n" + reference_inputs)
            
        requested_name = (specifics or {}).get("name")
        if requested_name:
            user.append(f'\nNAME OVERRIDE: requested logical name is "{requested_name}".')

        user.append("\nEmit the Terraform configuration now.")
        return [{"role": "system", "content": self.sys_prompt},
                {"role": "user", "content": "\n".join(user)}]

    def finalize_generation(self, raw: str) -> str:
        t = raw.strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[1] if "\n" in t else ""
            if t.rstrip().endswith("```"):
                t = t.rstrip()[:-3]
        return t.strip() + "\n"

    def render_module_call(self, module, project: str, env_tier: str, upstreams: List[str]) -> str:
        # No module calls in Flat TF, it generates resources directly.
        # But this method shouldn't typically be called if decision logic properly classifies it as net-new/flat.
        return f"# resource definition goes here\n"

    def scaffold_new_module(self, intent: str, project: str, nearest) -> str:
        rtype = self._guess_resource_type(intent)
        name = self._guess_resource_name(intent)
        ref = f"  # style reference: {nearest.key}" if nearest else ""
        env_var = "${var.environment}"

        files = {
            "main.tf":
                (f'# {intent}\n\n'
                 f'resource "{rtype}" "{name}" {{{ref}\n'
                 f'  # TODO: fill resource arguments\n'
                 f'  tags = {{\n'
                 f'    Name        = "{project}-{env_var}-{name}"\n'
                 f'    Project     = var.project\n'
                 f'    Environment = var.environment\n'
                 f'    ManagedBy   = "terraform"\n'
                 f'  }}\n'
                 f'}}\n'),
            "variables.tf":
                ('variable "project" {\n'
                 '  description = "Project name"\n'
                 '  type        = string\n'
                 '}\n\n'
                 'variable "environment" {\n'
                 '  description = "Environment (e.g. dev, staging, prod)"\n'
                 '  type        = string\n'
                 '}\n\n'
                 '# TODO: add resource-specific variables\n'),
            "outputs.tf":
                (f'output "{name}_id" {{\n'
                 f'  description = "ID of the {name} resource"\n'
                 f'  value       = {rtype}.{name}.id\n'
                 f'}}\n'),
            "backend.tf":
                ('terraform {\n'
                 '  # TODO: configure your backend\n'
                 '  # backend "s3" {\n'
                 '  #   bucket = "my-tf-state"\n'
                 '  #   key    = "terraform.tfstate"\n'
                 '  #   region = "us-east-1"\n'
                 '  # }\n'
                 '}\n'),
        }

        out = [f"# NET-NEW flat Terraform scaffold for: {intent}",
               f"# guessed resource type: {rtype}", ""]
        for path, body in files.items():
            out.append(f"# ===== {path} =====")
            out.append(body)
        return "\n".join(out)

    # --- resource type guessing ---
    _RESOURCE_TABLE = {
        "ec2": "aws_instance", "instance": "aws_instance", "vm": "aws_instance",
        "security group": "aws_security_group", "sg": "aws_security_group",
        "s3": "aws_s3_bucket", "bucket": "aws_s3_bucket",
        "rds": "aws_db_instance", "database": "aws_db_instance", "postgres": "aws_db_instance",
        "alb": "aws_lb", "load balancer": "aws_lb",
        "lambda": "aws_lambda_function", "function": "aws_lambda_function",
        "sqs": "aws_sqs_queue", "queue": "aws_sqs_queue",
        "sns": "aws_sns_topic", "topic": "aws_sns_topic",
        "iam role": "aws_iam_role", "role": "aws_iam_role",
        "vpc": "aws_vpc", "network": "aws_vpc",
        "subnet": "aws_subnet",
        "ecs": "aws_ecs_cluster", "ecs cluster": "aws_ecs_cluster",
        "dynamodb": "aws_dynamodb_table",
        "api gateway": "aws_api_gateway_rest_api",
        "resource group": "azurerm_resource_group",
        "aks": "azurerm_kubernetes_cluster", "kubernetes": "azurerm_kubernetes_cluster",
        "key vault": "azurerm_key_vault",
        "cosmos": "azurerm_cosmosdb_account",
        "gke": "google_container_cluster",
        "gcs": "google_storage_bucket",
    }

    def _guess_resource_type(self, intent: str) -> str:
        low = intent.lower()
        for k, v in sorted(self._RESOURCE_TABLE.items(), key=lambda x: len(x[0]), reverse=True):
            if k in low:
                return v
        return "UNKNOWN_RESOURCE"

    def _guess_resource_name(self, intent: str) -> str:
        rt = self._guess_resource_type(intent)
        if rt == "UNKNOWN_RESOURCE":
            words = intent.lower().split()
            skip = {"create", "provision", "deploy", "a", "an", "the", "new", "for", "in", "with"}
            meaningful = [w for w in words if w not in skip and len(w) > 2]
            return meaningful[0] if meaningful else "main"
        name = rt
        for prefix in ("aws_", "azurerm_", "google_", "digitalocean_"):
            name = name.replace(prefix, "")
        return name
