#!/bin/bash
# 删除旧容器和镜像，重新构建并运行

set -e

IMAGE_NAME="knowmart-backend:architecture-v2-dev"
CONTAINER_NAME="knowmart-backend"

echo "停止并删除旧容器..."
docker stop $CONTAINER_NAME 2>/dev/null || true
docker rm $CONTAINER_NAME 2>/dev/null || true

echo "删除旧镜像..."
docker rmi $IMAGE_NAME 2>/dev/null || true

echo "构建新镜像..."
cd "$(dirname "$0")/.."
docker build -t $IMAGE_NAME -f mybuild/Dockerfile .

echo "运行新容器..."
docker run --rm -d -p 8000:8000 --name $CONTAINER_NAME $IMAGE_NAME

echo "完成！容器正在运行，访问 http://localhost:8000"
echo "查看日志: docker logs -f $CONTAINER_NAME"
echo "停止容器: docker stop $CONTAINER_NAME"
