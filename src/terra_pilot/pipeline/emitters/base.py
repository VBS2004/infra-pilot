from abc import ABC, abstractmethod
from typing import Dict, List, Optional

class EmitterStrategy(ABC):
    @abstractmethod
    def build_llm_prompt(self, resource_type: str, specifics: Dict[str, object], grounding: str,
                         required: List[str], optional: List[str],
                         decision: str = "reuse", existing: bool = False,
                         existing_inputs: Optional[str] = None,
                         reference_inputs: Optional[str] = None,
                         reference_label: Optional[str] = None,
                         dest_ctx: Optional[Dict[str, object]] = None,
                         source_ctx: Optional[Dict[str, object]] = None,
                         repo: Optional[str] = None) -> List[Dict[str, str]]:
        pass
    
    @abstractmethod
    def finalize_generation(self, raw: str) -> str:
        """Process the raw text from the LLM into the final code string."""
        pass
        
    @abstractmethod
    def render_module_call(self, module, project: str, env_tier: str, upstreams: List[str]) -> str:
        """Render the code that calls/reuses a module (e.g. terragrunt.hcl or module{} block)."""
        pass
        
    @abstractmethod
    def scaffold_new_module(self, intent: str, project: str, nearest) -> str:
        """Scaffold a net-new leaf module."""
        pass
