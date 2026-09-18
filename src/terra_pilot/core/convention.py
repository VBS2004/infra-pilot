import dataclasses
import os
import re
from typing import List, Dict, Optional

@dataclasses.dataclass
class RepoConvention:
    kind: str                      # "terragrunt" | "tf-modules" | "tf-flat"
    root_dir: str                  # where the IaC lives
    module_dirs: List[str]         # discovered reusable module directories
    env_dirs: Dict[str, str]       # env_name -> directory (if env-based layout detected)
    var_files: List[str]           # tfvars / env-specific variable files
    has_remote_state: bool
    backend_type: str              # "s3", "local", "gcs", etc.
    provider: str                  # "aws", "google", "azurerm"
    confidence: str                # "high" | "medium" | "low"


def _find_files(repo_root: str, target_files: List[str], max_scan: int = 100) -> List[str]:
    found = []
    skip_dirs = {".terragrunt-cache", ".terraform", ".git", ".venv", "node_modules", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for f in filenames:
            if f in target_files:
                found.append(os.path.join(dirpath, f))
                if len(found) >= max_scan:
                    return found
    return found

def detect_convention(repo_root: str) -> RepoConvention:
    """Scan a repository to determine its Terraform/Terragrunt convention."""
    repo_root = os.path.abspath(repo_root)
    
    # 1. Terragrunt Detection
    tg_files = _find_files(repo_root, ["terragrunt.hcl"])
    if len(tg_files) > 0:
        return RepoConvention(
            kind="terragrunt",
            root_dir=repo_root,
            module_dirs=[],
            env_dirs={},
            var_files=[],
            has_remote_state=True,
            backend_type="",
            provider="",
            confidence="high"
        )
        
    # 2. Plain TF Modules Detection
    # Look for module { ... } blocks in .tf files
    tf_files = _find_files(repo_root, ["main.tf", "variables.tf", "infrastructure.tf"], max_scan=500)
    has_modules = False
    module_dirs = []
    for tf_file in tf_files:
        try:
            with open(tf_file, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
                if re.search(r'\bmodule\s+"[^"]+"\s*\{', content):
                    has_modules = True
                    break
        except Exception:
            pass
            
    # Also check if there's a 'modules/' directory
    modules_path = os.path.join(repo_root, "modules")
    if os.path.isdir(modules_path):
        has_modules = True
        module_dirs.append(modules_path)

    if has_modules:
        return RepoConvention(
            kind="tf-modules",
            root_dir=repo_root,
            module_dirs=module_dirs,
            env_dirs={},
            var_files=_find_files(repo_root, ["terraform.tfvars"], max_scan=50),
            has_remote_state=False,
            backend_type="",
            provider="",
            confidence="high"
        )
        
    # 3. Flat TF
    if len(tf_files) > 0:
        return RepoConvention(
            kind="tf-flat",
            root_dir=repo_root,
            module_dirs=[],
            env_dirs={},
            var_files=_find_files(repo_root, ["terraform.tfvars"], max_scan=50),
            has_remote_state=False,
            backend_type="",
            provider="",
            confidence="medium"
        )
        
    # Fallback
    return RepoConvention(
        kind="tf-flat",
        root_dir=repo_root,
        module_dirs=[],
        env_dirs={},
        var_files=[],
        has_remote_state=False,
        backend_type="",
        provider="",
        confidence="low"
    )
