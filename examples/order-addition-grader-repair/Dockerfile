FROM seededge/edgebench.judge.order_addition_permutation_optimization@sha256:6d1a1506f52a5cc7c95680c6df7cd72cb95f81fa236b9794aa507490b947fbd8

USER root
COPY repair.py verify.py /tmp/order-addition-repair/
RUN cp /home/workspace/complex_job_scheduling/tests/test_final_result.py /tmp/order-addition-repair/original.py \
    && python3 /tmp/order-addition-repair/repair.py \
        /tmp/order-addition-repair/original.py \
        /home/workspace/complex_job_scheduling/tests/score_vcpom_result.py \
        /home/workspace/complex_job_scheduling/score_vcpom_result.py \
        /tmp/order-addition-repair/repaired.py \
    && cp /tmp/order-addition-repair/repaired.py /home/workspace/complex_job_scheduling/tests/test_final_result.py \
    && python3 /tmp/order-addition-repair/verify.py \
        /home/workspace/complex_job_scheduling /tmp/order-addition-repair/original.py \
    && rm -rf /tmp/order-addition-repair
