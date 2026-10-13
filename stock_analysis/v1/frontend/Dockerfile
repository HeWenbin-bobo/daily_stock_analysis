# 前端多阶段构建
FROM node:20-alpine AS builder

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# 运行时 - 用 nginx 提供静态文件
FROM nginx:1.27-alpine

# nginx:alpine 不含 wget, 健康检查需要, 单独安装
RUN apk add --no-cache wget

# 复制构建产物
COPY --from=builder /app/dist /usr/share/nginx/html

# 自定义 nginx 配置 (代理 /api 到后端)
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 5179

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost:5179/ || exit 1
