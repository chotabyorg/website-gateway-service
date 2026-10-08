from pathlib import Path

import yaml


def test_website_image_publishers_use_isolated_gitops_release_branch():
    """A source merge may stage an image, but must not change production main."""
    workflow_dir = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    publishers = []
    for path in sorted(workflow_dir.iterdir()):
        if path.suffix not in {".yaml", ".yml"}:
            continue
        # BaseLoader preserves GitHub's `on` key instead of YAML 1.1 booleans.
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        for name, job in workflow.get("jobs", {}).items():
            if not job.get("uses", "").startswith(
                "chotabyorg/gitops/.github/workflows/reusable-deploy.yaml@"
            ):
                continue
            publishers.append((path.name, name))
            inputs = job.get("with", {})
            assert inputs.get("service") == "website-gateway-service"
            assert inputs.get("gitops_repo") == "chotabyorg/gitops"
            assert inputs.get("gitops_branch") == "releases/website-gateway-service", (
                f"{path.name}:{name} must stage its image outside GitOps main; "
                "an omitted branch defaults to production main"
            )
    assert publishers, "Expected a Website image publisher to validate"
