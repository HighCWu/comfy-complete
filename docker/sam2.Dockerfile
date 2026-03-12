ARG BASE_IMAGE
FROM $BASE_IMAGE AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ARG SAM2_COMMIT

RUN pip wheel \
  --no-build-isolation \
  --no-deps \
  "sam-2 @ https://github.com/facebookresearch/sam2/archive/$SAM2_COMMIT.tar.gz" \
  -w /wheels

FROM scratch

COPY --from=builder /wheels /wheels
