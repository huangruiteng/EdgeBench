"""Exercise the original and repaired image with a synthetic public-API result.

Run during the image build. All mutations occur in temporary copies; no solver
result, private test source or reference permutation is shipped in this recipe.
"""
import importlib.util
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from repair import HELPER_SHA256, OLD_HELPER_SHA256, repair


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_pytest(project, expected_passed, expected_failed):
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_final_result.py", "-q"],
        cwd=project, text=True, capture_output=True, timeout=60,
    )
    output = completed.stdout + completed.stderr
    expected_code = 1 if expected_failed else 0
    if completed.returncode != expected_code or f"{expected_passed} passed" not in output:
        raise AssertionError(output)
    if expected_failed and f"{expected_failed} failed" not in output:
        raise AssertionError(output)
    return output


def verify(project, original_path):
    original = original_path.read_bytes()
    private = (project / "tests/score_vcpom_result.py").read_bytes()
    public = (project / "score_vcpom_result.py").read_bytes()
    repaired = (project / "tests/test_final_result.py").read_bytes()
    assert repair(original, private, public) == repaired
    assert repaired.replace(HELPER_SHA256.encode(), OLD_HELPER_SHA256.encode(), 1) == original
    for values in [(original + b"\n", private, public),
                   (original, private + b"\n", public),
                   (original, private, public + b"\n")]:
        try:
            repair(*values)
        except ValueError:
            pass
        else:
            raise AssertionError("Unrecognized source/helper was accepted")

    scorer = load_module("public_scorer", project / "score_vcpom_result.py")
    cost_ref, num_ref = 200690260.6677975, 200000
    for cost_factor, num_factor in [(1, 1), (.9, .5), (2, 1), (1, 4), (2, 4)]:
        expected = 100 * min(1, 1 / cost_factor) ** 3 * min(1, 1 / num_factor) ** .5
        assert math.isclose(scorer.score_cost_num(cost_ref * cost_factor,
                                                int(num_ref * num_factor)), expected)

    with tempfile.TemporaryDirectory(prefix="order-judge-regression-") as directory:
        target = Path(directory)
        (target / "tests").mkdir()
        for name in ["vcpom_m1000_final_result.txt", "score_vcpom_result.py",
                     "cost_complex_cy.cpython-311-x86_64-linux-gnu.so",
                     "tests/score_vcpom_result.py",
                     "tests/cost_complex_cy.cpython-311-x86_64-linux-gnu.so"]:
            shutil.copy2(project / name, target / name)
        cost_module = load_module("cost_complex_cy", target / "cost_complex_cy.cpython-311-x86_64-linux-gnu.so")
        permutation = list(range(1, 1001))
        cost = cost_module.cost_complex_with_builtin_data(permutation, m=1000, seed=1)
        (target / "final_result.txt").write_text(
            "final_pai=" + ",".join(map(str, permutation)) + f"\nfinal_cost={cost!r}\nfinal_num=1\n"
        )
        test = target / "tests/test_final_result.py"
        test.write_bytes(original)
        before = run_pytest(target, 4, 1)
        assert "test_private_judge_files_are_available" in before
        test.write_bytes(repaired)
        # Clear bytecode between equal-size source rewrites within one timestamp.
        shutil.rmtree(target / "tests/__pycache__", ignore_errors=True)
        run_pytest(target, 5, 0)
        for name in ["tests/score_vcpom_result.py",
                     "tests/cost_complex_cy.cpython-311-x86_64-linux-gnu.so",
                     "vcpom_m1000_final_result.txt"]:
            asset = target / name
            previous = asset.read_bytes()
            try:
                asset.write_bytes(previous + b"\n")
                run_pytest(target, 4, 1)
            finally:
                asset.write_bytes(previous)
        # The scoring check still rejects a fabricated reported cost.
        result = target / "final_result.txt"
        result.write_text(result.read_text().replace(f"final_cost={cost!r}", "final_cost=1.0"))
        run_pytest(target, 4, 1)
    print(json.dumps({"original": "4 passed, 1 failed", "repaired": "5 passed",
                      "negative_asset_and_result_cases": 4,
                      "unsupported_build_inputs_rejected": 3,
                      "public_score_formula_cases": 5}))


if __name__ == "__main__":
    verify(Path(sys.argv[1]), Path(sys.argv[2]))
