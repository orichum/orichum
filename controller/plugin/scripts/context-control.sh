#!/usr/bin/env bash
set -eu
exec "${ORICHUM_PYTHON:?}" -I -B \
  "${CLAUDEX_WORKFLOW_ROOT:?}/integrations/common/context_control.py"
