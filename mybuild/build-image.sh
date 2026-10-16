#!/bin/bash

# Unified Backend 镜像构建脚本
# 用法: ./build-image.sh [--tag TAG]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info()    { echo -e "${BLUE}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $(date '+%Y-%m-%d %H:%M:%S') $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $1"; }

BUILD_CONF="${SCRIPT_DIR}/build.conf"
if [ ! -f "$BUILD_CONF" ]; then
    log_error "构建配置文件不存在: $BUILD_CONF"
    exit 1
fi
log_info "加载构建配置: $BUILD_CONF"
source "$BUILD_CONF"
source "$SCRIPT_DIR/release-tag.sh"
candidate_tag="${BACKEND_TAG:-architecture-v2-dev}"
if [[ "${1:-}" == "--tag" && -n "${2:-}" ]]; then candidate_tag="$2"; fi
require_development_tag "$candidate_tag" || exit 1
source "$SCRIPT_DIR/harbor-cluster.sh"
REGISTRY="$(resolve_k8s_images_registry)" || exit 1
export REGISTRY

BACKEND_IMAGE="${BACKEND_IMAGE:-knowmart-backend}"
BACKEND_TAG="${BACKEND_TAG:-architecture-v2-dev}"
BACKEND_IMAGE_REGISTRY="${BACKEND_IMAGE_REGISTRY:-harbor.sunmoonai.com}"
BACKEND_IMAGE_PROJECT="${BACKEND_IMAGE_PROJECT:-app-images}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"
PUSH_IMAGES_AFTER_BUILD="${PUSH_IMAGES_AFTER_BUILD:-false}"

CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-docker}"
if [[ "$CONTAINER_RUNTIME" == "sudo nerdctl" || "$CONTAINER_RUNTIME" == "nerdctl" ]]; then
    NERDCTL_NAMESPACE="${NERDCTL_NAMESPACE:-k8s.io}"
    RUNTIME_CMD="sudo nerdctl -n ${NERDCTL_NAMESPACE}"
    if ! command -v nerdctl &> /dev/null; then log_error "nerdctl 未安装"; exit 1; fi
else
    RUNTIME_CMD="docker"
    if ! command -v docker &> /dev/null; then log_error "docker 未安装"; exit 1; fi
fi

if [[ "$1" == "--tag" && -n "$2" ]]; then
    BACKEND_TAG="$2"
    log_info "使用自定义标签: $BACKEND_TAG"
fi

PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ensure_base_image() {
    local public_image="$1"
    local harbor_image="${REGISTRY}/${public_image}"
    log_info "检查基础镜像: ${harbor_image}"
    if $RUNTIME_CMD image inspect "${harbor_image}" > /dev/null 2>&1; then
        log_info "本地基础镜像已就绪: ${harbor_image}"
        return 0
    fi
    if $RUNTIME_CMD pull "${harbor_image}" > /dev/null 2>&1; then
        log_info "基础镜像已就绪: ${harbor_image}"
    else
        log_error "Harbor 中不存在基础镜像: ${harbor_image}"
        log_error "请先将该镜像推送到 Harbor，参考 k8s 目录下的镜像推送脚本"
        exit 1
    fi
}

push_image() {
    local push_registry
    push_registry="$(resolve_harbor_registry_for_push "${BACKEND_IMAGE_REGISTRY:-harbor.sunmoonai.com}")"
    FULL_IMAGE_NAME="${push_registry}/${BACKEND_IMAGE_PROJECT}/${BACKEND_IMAGE}:${BACKEND_TAG}"
    log_info "推送镜像: $FULL_IMAGE_NAME"
    $RUNTIME_CMD tag "${BACKEND_IMAGE}:${BACKEND_TAG}" "$FULL_IMAGE_NAME"
    if push_image_with_harbor_verify "$RUNTIME_CMD" "$FULL_IMAGE_NAME"; then
        log_success "✅ 推送成功: $FULL_IMAGE_NAME"
    else
        log_error "❌ 推送失败: $FULL_IMAGE_NAME"
        exit 1
    fi
}

build_image() {
    log_info "开始构建镜像: ${BACKEND_IMAGE}:${BACKEND_TAG}"
    log_info "Dockerfile: $SCRIPT_DIR/$DOCKERFILE"
    log_info "构建上下文: $PROJECT_ROOT"

    cd "$PROJECT_ROOT"
    $RUNTIME_CMD build -f "$SCRIPT_DIR/$DOCKERFILE" \
        -t "${BACKEND_IMAGE}:${BACKEND_TAG}" \
        --build-arg REGISTRY="${REGISTRY}" \
        --build-arg PYPI_INDEX_URL="${PYPI_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
        --build-arg DEBIAN_MIRROR="${DEBIAN_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/debian}" \
        --build-arg DEBIAN_SECURITY_MIRROR="${DEBIAN_SECURITY_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/debian-security}" \
        --build-arg HTTP_PROXY="${HTTP_PROXY:-}" \
        --build-arg HTTPS_PROXY="${HTTPS_PROXY:-}" \
        --build-arg NO_PROXY="${NO_PROXY:-localhost,127.0.0.1}" \
        .

    log_success "镜像构建完成: ${BACKEND_IMAGE}:${BACKEND_TAG}"

    if [[ "${PUSH_IMAGES_AFTER_BUILD}" == "true" ]]; then
        push_image
        log_success "✅ 构建并推送完成"
    else
        log_info "PUSH_IMAGES_AFTER_BUILD=false，跳过推送"
    fi
    cd - > /dev/null
}

log_info "Unified Backend 镜像构建脚本启动"
ensure_base_image "python:3.12-slim"
build_image
