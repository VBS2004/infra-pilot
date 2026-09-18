from .base import EmitterStrategy

def get_emitter(convention_kind: str) -> EmitterStrategy:
    from .terragrunt import TerragruntEmitter
    from .plain_tf import PlainTFEmitter
    from .flat_tf import FlatTFEmitter
    
    if convention_kind == "terragrunt":
        return TerragruntEmitter()
    elif convention_kind == "tf-modules":
        return PlainTFEmitter()
    elif convention_kind == "tf-flat":
        return FlatTFEmitter()
    return TerragruntEmitter() # fallback
