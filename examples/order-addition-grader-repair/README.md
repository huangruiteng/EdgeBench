# Order Addition judge checksum repair

The published judge image `seededge/edgebench.judge.order_addition_permutation_optimization:f6f385925889`
contains identical public and private `score_vcpom_result.py` files, but its
test expects an older helper checksum. A valid result therefore passes only
four of five tests. With `pass_rate_first`, this can also prevent a later
better score from replacing an earlier result tied at an incomplete pass rate.

This build recipe changes only the fixed `EXPECTED_SCORE_HELPER_SHA256`
declaration in the known test revision. It verifies the original test and both
helper digests before writing. The helper bytes, published scoring formula,
cost module, reference file and all assertions remain unchanged. An unexpected
input revision fails the build; no checksum is learned from submitted files.

The task definitions are published on Hugging Face and the evaluator source is
distributed in the Docker image. This follows the adjacent Portfolio repair
recipe: a reproducible image repair lives here without copying hidden tests,
private data, reference solutions or solver trajectories into this repository.

```sh
docker build --network=none \
  -t seededge/edgebench.judge.order_addition_permutation_optimization:checksum-v1 \
  examples/order-addition-grader-repair
```

The build pins the published image digest and runs regressions in temporary
workspaces. An identity permutation, evaluated once through the public cost
API, reproduces the original failure and passes all five corrected tests.
Tampered helper, cost module, reference file and reported cost remain rejected.
Additional checks reject unrecognized build inputs and verify the public score
formula at its reference, saturation and penalty boundaries. No generated
`final_result.txt` is left in the resulting image.

For new evaluations, copy the task manifest and change only
`judge.image_tag` to `checksum-v1`, retaining the default `seededge` registry.
Record the resulting image ID/digest and repair source commit with the copied
manifest. The source dataset, existing image tag and running experiments are
not modified by this recipe. Image publication and dataset pin updates are
separate rollout operations.

Historical rescoring must use a separate result directory and the same retained
submission archives for every compared arm. Preserve original scores, capture
times and failure classifications. Once current solvers finish, rescore all
retained submissions needed to reconstruct selection under the corrected judge;
rescoring only the old retained best cannot recover a better rejected candidate.
