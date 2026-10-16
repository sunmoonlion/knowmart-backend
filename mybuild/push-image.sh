#!/bin/bash

# Unified Backend 镜像推送脚本
# 用法: ./push-image.sh [--tag TAG]
# 凭证: 优先读取 HARBOR_USER/HARBOR_PASSWORD 或 k8s Harbor 配置，缺失时提示输入

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info()    { echo -e "${BLUE}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $(date '+%Y-%m-%d %H:%M:%S') $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $(date '+%Y-%m-%d %H:%M:%S') $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $1"; }

BUILD_CONF="${SCRIPT_DIR}/build.conf"
if [ ! -f "$BUILD_CONF" ]; then log_error "build.conf 不存在"; exit 1; fi
source "$BUILD_CONF"
source "$SCRIPT_DIR/release-tag.sh"
candidate_tag="${BACKEND_TAG:-architecture-v2-dev}"
if [[ "${1:-}" == "--tag" && -n "${2:-}" ]]; then candidate_tag="$2"; fi
require_development_tag "$candidate_tag" || exit 1
source "$SCRIPT_DIR/harbor-cluster.sh"

IMAGE_NAME="${BACKEND_IMAGE:-knowmart-backend}"
IMAGE_TAG="${BACKEND_TAG:-architecture-v2-dev}"
IMAGE_REGISTRY="$(resolve_harbor_registry_for_push "${BACKEND_IMAGE_REGISTRY:-harbor.sunmoonai.com}")"
IMAGE_PROJECT="${BACKEND_IMAGE_PROJECT:-app-images}"

CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-docker}"
if [[ "$CONTAINER_RUNTIME" == "sudo nerdctl" || "$CONTAINER_RUNTIME" == "nerdctl" ]]; then
    RUNTIME_CMD="sudo nerdctl -n ${NERDCTL_NAMESPACE:-k8s.io}"
else
    RUNTIME_CMD="docker"
fi

if [[ "$1" == "--tag" && -n "$2" ]]; then IMAGE_TAG="$2"; fi

# ── Harbor 凭证（优先从环境变量和 k8s 配置读取，缺失时才提示输入）────────────────
load_harbor_credentials_for_push
_harbor_user="${HARBOR_USER}"
_harbor_pass="${HARBOR_PASSWORD}"

ensure_harbor_project() {
    local registry="$1"
    local project="$2"

    log_info "检查 Harbor 项目: ${project}"
    local http_code
    http_code=$(curl -sk -o /dev/null -w "%{http_code}" \
        -u "${_harbor_user}:${_harbor_pass}" \
        "https://${registry}/api/v2.0/projects/${project}")

    if [ "$http_code" = "200" ]; then
        log_info "Harbor 项目已存在: ${project}"
    elif [ "$http_code" = "404" ]; then
        log_info "Harbor 项目不存在，正在创建: ${project}"
        local create_code
        create_code=$(curl -sk -o /dev/null -w "%{http_code}" \
            -u "${_harbor_user}:${_harbor_pass}" \
            -X POST "https://${registry}/api/v2.0/projects" \
            -H "Content-Type: application/json" \
            -d "{\"project_name\":\"${project}\",\"public\":false,\"metadata\":{\"public\":\"false\"}}")
        if [ "$create_code" = "201" ] || [ "$create_code" = "409" ]; then
            log_success "Harbor 项目就绪: ${project}"
        else
            log_error "Harbor 项目创建失败，HTTP ${create_code}"
            exit 1
        fi
    else
        log_warn "Harbor API 返回 HTTP ${http_code}，跳过项目检查直接推送"
    fi
}

FULL_IMAGE_NAME="${IMAGE_REGISTRY}/${IMAGE_PROJECT}/${IMAGE_NAME}:${IMAGE_TAG}"
LOCAL_IMAGE_NAME="${IMAGE_NAME}:${IMAGE_TAG}"

log_info "推送镜像: ${LOCAL_IMAGE_NAME} → ${FULL_IMAGE_NAME}"

if ! $RUNTIME_CMD images | grep -q "${IMAGE_NAME}.*${IMAGE_TAG}"; then
    log_error "本地镜像不存在: ${LOCAL_IMAGE_NAME}，请先运行 ./build-image.sh"
    exit 1
fi

ensure_harbor_project "${IMAGE_REGISTRY}" "${IMAGE_PROJECT}"

$RUNTIME_CMD tag "${LOCAL_IMAGE_NAME}" "${FULL_IMAGE_NAME}"

if push_image_with_harbor_verify "$RUNTIME_CMD" "$FULL_IMAGE_NAME"; then
    log_success "✅ 推送成功: ${FULL_IMAGE_NAME}"
else
    log_error "❌ 推送失败，请确认已登录: ${RUNTIME_CMD} login ${IMAGE_REGISTRY}"
    exit 1
fi
