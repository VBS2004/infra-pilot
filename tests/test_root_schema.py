import os
import tempfile
import textwrap
import unittest

import root_schema


class TestRootSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _write_file(self, rel_path, content):
        path = os.path.join(self.repo_root, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(textwrap.dedent(content).strip() + "\n")
        return path

    def test_fallback_when_no_files(self):
        schema = root_schema.parse(self.repo_root)
        self.assertEqual(schema.confidence, "fallback")
        self.assertEqual(schema.include_target, "root.hcl")
        self.assertEqual(schema.module_source_template, "modules/{provider}/infrastructure/{terraform_module}/{component}")

    def test_legacy_terragrunt_hcl(self):
        # A component that uses legacy no-arg find_in_parent_folders
        self._write_file("payments/live/nonprod/ec2/terragrunt.hcl", """
            include "root" {
              path = find_in_parent_folders()
            }
        """)
        # The root file it finds
        self._write_file("payments/live/terragrunt.hcl", """
            remote_state {
              backend = "s3"
            }
            generate "provider" {
              contents = <<EOF
                provider "aws" {}
              EOF
            }
        """)
        
        schema = root_schema.parse(self.repo_root)
        self.assertEqual(schema.confidence, "partial")
        self.assertEqual(schema.include_target, "terragrunt.hcl")
        self.assertEqual(schema.root_file_name, "terragrunt.hcl")
        self.assertEqual(schema.backend_type, "s3")
        self.assertEqual(schema.provider_name, "aws")

    def test_modern_root_hcl_full_discovery(self):
        self._write_file("project/aws/dev/component/terragrunt.hcl", """
            include "root" {
              path = find_in_parent_folders("root.hcl")
            }
        """)
        self._write_file("root.hcl", """
            locals {
              env_cfg = yamldecode(file(find_in_parent_folders("config.yml")))
            }
            terraform {
              source = "${get_parent_terragrunt_dir()}/modules//aws/infrastructure/${local.env_cfg.terraform_module}/${basename(get_original_terragrunt_dir())}/"
            }
        """)
        
        schema = root_schema.parse(self.repo_root)
        self.assertEqual(schema.confidence, "full")
        self.assertEqual(schema.include_target, "root.hcl")
        self.assertEqual(schema.env_config_filename, "config.yml")
        self.assertEqual(schema.env_config_fields, ["terraform_module"])
        self.assertEqual(schema.module_source_template, "modules/aws/infrastructure/{terraform_module}/{component}")

    def test_custom_account_hcl(self):
        self._write_file("tier1/component/terragrunt.hcl", """
            include {
              path = find_in_parent_folders("account.hcl")
            }
        """)
        self._write_file("account.hcl", """
            locals {
              env_cfg = yamldecode(file(find_in_parent_folders("env-settings.yaml")))
              req_field1 = local.env_cfg.region
              req_field2 = local.env_cfg.account_id
            }
        """)
        
        schema = root_schema.parse(self.repo_root)
        self.assertEqual(schema.confidence, "partial")
        self.assertEqual(schema.include_target, "account.hcl")
        self.assertEqual(schema.root_file_name, "account.hcl")
        self.assertEqual(schema.env_config_filename, "env-settings.yaml")
        # should deduce required fields
        self.assertCountEqual(schema.env_config_fields, ["region", "account_id"])

if __name__ == "__main__":
    unittest.main()
