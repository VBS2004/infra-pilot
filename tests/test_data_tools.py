"""Data / model tooling that had no tests: cpt_data, the local (air-gapped)
generator, and the PySpark pipelines. Heavy parts skip themselves when their
dependency (torch+transformers, pyspark+Java) is not installed, so default CI
stays light; run locally with the full venv to exercise everything."""
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile

import harness
from harness import check, copy_fixture, captured, finish, FIXTURES, ROOT, point_at
from fake_gateway import FakeGateway

sys.path.insert(0, ROOT)          # for the top-level data_pipeline package


def skip(msg):
    print(f"  skip {msg}")


# --------------------------------------------------------------------------- #
def test_cpt_dataset():
    print("\n# cpt_data: JSONL for continued pretraining")
    from terra_pilot.models import cpt_data
    repo = os.path.join(FIXTURES, "myrepo")
    out = os.path.join(tempfile.mkdtemp(), "cpt.jsonl")
    with captured():
        n = cpt_data.build_dataset(repo, out, 400_000, False)
    rows = [json.loads(l) for l in open(out, encoding="utf-8")]
    texts = [r["text"] for r in rows]
    check("one JSON object per line, all with 'text'", n == len(rows) > 5 and all(list(r) == ["text"] for r in rows))
    check("terragrunt.hcl (generated) is skipped", not any("terragrunt.hcl" in t for t in texts))
    ec2 = next(t for t in texts if "infrastructure/env/ec2" in t.split("\n", 1)[0] or "env/ec2/variables.tf" in t)
    check("module files grouped: variables.tf before main.tf",
          ec2.index("variables.tf") < ec2.index("main.tf"), ec2[:200])
    check("inputs.hcl examples present", any("# file:" in t and "inputs.hcl" in t for t in texts))

    tiny = os.path.join(tempfile.mkdtemp(), "t.jsonl")
    with captured():
        n2 = cpt_data.build_dataset(repo, tiny, 300, False)
    chunks = [json.loads(l)["text"] for l in open(tiny, encoding="utf-8")]
    check("small max-chars splits into more examples", n2 > n)
    check("chunks respect max-chars (allowing one closing block of slack)", max(len(c) for c in chunks) < 1200)
    with captured():
        n3 = cpt_data.build_dataset(repo, tiny, 400_000, True)
    check("--no-grouping gives one example per file", n3 > n)
    check("chunked HCL keeps braces balanced",
          all(c.count("{") == c.count("}") for c in chunks if "::part" not in c and len(c) < 250))


def _tiny_model_dir():
    from tokenizers import Tokenizer, models, pre_tokenizers, trainers
    from transformers import GPT2Config, GPT2LMHeadModel, PreTrainedTokenizerFast
    d = tempfile.mkdtemp(prefix="tp_tinylm_")
    tok = Tokenizer(models.WordLevel(unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    corpus = ["inputs = { ami_id = \"ami\" instance_type = \"t3.micro\" }",
              "system user assistant emit inputs hcl now request"] * 4
    tok.train_from_iterator(corpus, trainers.WordLevelTrainer(special_tokens=["[UNK]", "[PAD]", "[EOS]"]))
    fast = PreTrainedTokenizerFast(tokenizer_object=tok, unk_token="[UNK]", pad_token="[PAD]", eos_token="[EOS]")
    fast.save_pretrained(d)
    cfg = GPT2Config(vocab_size=tok.get_vocab_size(), n_embd=16, n_layer=1, n_head=2, n_positions=16384,
                     eos_token_id=fast.eos_token_id, bos_token_id=fast.eos_token_id)
    GPT2LMHeadModel(cfg).save_pretrained(d)
    return d


def test_local_generator():
    print("\n# local_generator: air-gapped transformers path (tiny random model, CPU)")
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        import tokenizers  # noqa: F401
    except Exception as e:
        return skip(f"torch/transformers not installed ({type(e).__name__})")
    from terra_pilot.llm import local_generator
    from terra_pilot.pipeline import compose
    d = _tiny_model_dir()
    os.environ.update({"LOCAL_MODEL": d, "LOCAL_DEVICE": "cpu",
                       "TRANSFORMERS_OFFLINE": "1", "HF_HUB_OFFLINE": "1"})
    try:
        msgs = [{"role": "system", "content": "Output only HCL."}, {"role": "user", "content": "emit inputs now"}]
        out = local_generator.complete(msgs, max_tokens=8, temperature=0.0)
        check("complete() returns text", isinstance(out, str))
        check("model loaded on the requested device", str(local_generator._MODEL.device) == "cpu")
        out2 = local_generator.complete(msgs, max_tokens=8, temperature=0.0)
        check("greedy decoding is deterministic", out == out2)

        gw = FakeGateway(lambda k, m: "SHOULD NOT BE CALLED").start()
        point_at(gw)
        real_complete = local_generator.complete     # cap length: compose asks for 8192 tokens
        local_generator.complete = lambda m, **kw: real_complete(m, **{**kw, "max_tokens": 16})
        try:
            repo = copy_fixture("myrepo")
            a = compose.compose(repo, resource_type="ec2", project="auth", env="prod")
        finally:
            local_generator.complete = real_complete
            gw.stop()
        check("LOCAL_MODEL routes compose generation locally (gateway untouched)",
              len(gw.chat_calls("generate")) == 0)
        check("output still finalized into an inputs block", a.inputs_hcl.lstrip().startswith("inputs"))
    finally:
        for k in ("LOCAL_MODEL", "LOCAL_DEVICE"):
            os.environ.pop(k, None)
    os.environ["LOCAL_MODEL"] = "/nonexistent/model/dir"
    local_generator._MODEL = None
    try:
        local_generator.complete([{"role": "user", "content": "x"}])
        check("missing model dir raises a clear error", False)
    except RuntimeError as e:
        check("missing model dir raises a clear error", "not a directory" in str(e))
    finally:
        os.environ.pop("LOCAL_MODEL", None)


# --------------------------------------------------------------------------- #
def _tgz(path, files):
    with tarfile.open(path, "w:gz") as tar:
        for name, text in files.items():
            data = text.encode("utf-8")
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))


def test_extract_archive_unit():
    print("\n# data_pipeline.extract_files.extract_archive (no Spark)")
    from data_pipeline import extract_files as ef
    d = tempfile.mkdtemp()
    good = 'resource "aws_s3_bucket" "b" {\n  bucket = "x"\n}\n'
    p = os.path.join(d, "42.tar.gz")
    _tgz(p, {"42/main.tf": good, "42/README.md": "# hi", "42/.terraform/x.tf": good,
             "42/broken.tf": 'resource "a" "b" {\n', "42/creds.tf": 'k = "AKIAABCDEFGHIJKLMNOP"\n',
             "42/bin.tf": "a\x00b"})
    rows = {r[1]: r for r in ef.extract_archive(p)}
    check("only HCL files outside skip-dirs, no binary", sorted(rows) == ["42/broken.tf", "42/creds.tf", "42/main.tf"], str(sorted(rows)))
    check("repo id parsed from the archive name", rows["42/main.tf"][0] == 42)
    check("parse_ok flags structural errors", rows["42/main.tf"][6] and not rows["42/broken.tf"][6])
    check("secret detector flags an AWS key", rows["42/creds.tf"][7] and not rows["42/main.tf"][7])
    bad = os.path.join(d, "7.tar.gz")
    open(bad, "wb").write(b"not a tarball")
    check("corrupt archive yields no rows instead of raising", ef.extract_archive(bad) == [])


def _spark_or_skip():
    try:
        import pyspark  # noqa: F401
        import pandas  # noqa: F401
        from data_pipeline.spark_env import get_spark
        return get_spark("terra-pilot-test", master="local[2]", shuffle_partitions=2)
    except Exception as e:
        skip(f"pyspark/Java unavailable ({type(e).__name__}: {str(e)[:60]})")
        return None


def test_spark_pipelines(spark):
    print("\n# PySpark pipelines on synthetic data")
    from data_pipeline import extract_files as ef, build_metadata as bm
    d = tempfile.mkdtemp()
    a = 'resource "aws_s3_bucket" "b" {\n  bucket = "shared"\n}\n'
    _tgz(os.path.join(d, "1.tar.gz"), {"1/main.tf": a, "1/vars.tf": 'variable "x" {}\n', "1/broken.tf": "a = {\n"})
    _tgz(os.path.join(d, "2.tar.gz"), {"2/main.tf": a, "2/leak.tf": 'k = "AKIAABCDEFGHIJKLMNOP"\n',
                                       "2/e.tfvars": 'region = "us-east-1"\n'})
    _tgz(os.path.join(d, "3.tar.gz"), {"3/notes.md": "x"})
    out = os.path.join(d, "out.parquet")
    c = ef.run(d, out, spark=spark, partitions=2)
    check("archives counted", c["archives"] == 3)
    check("files extracted (broken + leak included until filtered)", c["files"] == 6, str(c))
    check("structurally broken HCL dropped", c["parse_ok"] == 5, str(c))
    check("secret-bearing file dropped", c["no_secret"] == 4, str(c))
    check("exact duplicate collapsed (same content in 2 repos)", c["unique"] == 3, str(c))
    rows = spark.read.parquet(out).collect()
    kept = {(r["repo_id"], r["path"]) for r in rows}
    check("dedup keeps the lowest repo_id", (1, "1/main.tf") in kept and (2, "2/main.tf") not in kept, str(kept))
    check("output partitioned by extension", os.path.isdir(os.path.join(out, "ext=.tf"))
          and os.path.isdir(os.path.join(out, "ext=.tfvars")))

    db = os.path.join(d, "t.sqlite")
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE Repositories(Id INTEGER, FullName TEXT, License TEXT, StarCount INTEGER, Archived INTEGER, SizeInKb INTEGER);
      CREATE TABLE Modules(Id INTEGER, RepositoryId INTEGER, Path TEXT);
      CREATE TABLE Resources(Id INTEGER, ModuleId INTEGER, ResourceType TEXT, Provider TEXT);
      INSERT INTO Repositories VALUES (1,'a/x','MIT',5,0,100),(2,'a/x','MIT',9,0,100),(3,'b/y','GPL-3.0',1,0,100),
                                      (4,'c/z','MIT',1,1,100),(5,'d/w','Apache-2.0',1,0,3),(6,'e/v','BSD-3-Clause',1,0,50);
      INSERT INTO Modules VALUES (10,1,'m'),(11,2,'m'),(12,6,'m'),(13,3,'m');
      INSERT INTO Resources VALUES (100,10,'aws_s3_bucket','aws'),(101,11,'aws_s3_bucket','aws'),
                                   (102,12,'aws_instance','aws'),(103,13,'aws_vpc','aws');
    """)
    con.commit(); con.close()
    outdir = tempfile.mkdtemp()
    m = bm.build(db, outdir, spark=spark)
    check("license filter", m["licensed"] == 5, str(m))
    check("archived dropped", m["active"] == 4, str(m))
    check("duplicate FullName collapsed to the most-starred repo", m["deduped"] == 3, str(m))
    check("size filter", m["sized"] == 2, str(m))
    check("join keeps only modules/resources of surviving repos", m["modules"] == 2 and m["resources"] == 2, str(m))
    kept_repos = {r["Id"] for r in spark.read.parquet(os.path.join(outdir, "terrads_filtered.parquet")).collect()}
    check("deterministic dedupe kept repo 2 (9 stars) not repo 1", kept_repos == {2, 6}, str(kept_repos))
    joined = spark.read.parquet(os.path.join(outdir, "terrads_joined.parquet"))
    check("single ModuleId column after the join", joined.columns.count("ModuleId") == 1)


if __name__ == "__main__":
    test_cpt_dataset()
    test_local_generator()
    test_extract_archive_unit()
    sp = _spark_or_skip()
    if sp is not None:
        try:
            test_spark_pipelines(sp)
        finally:
            sp.stop()
    finish()
