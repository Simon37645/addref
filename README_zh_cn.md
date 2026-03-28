# AddRef

[English](README.md) | [简体中文](README_zh_cn.md)

AddRef 是一个本地部署的 Web 工具，用于给医学和生命科学文本自动补充 PubMed 参考文献。

## 功能

- 支持 OpenAI-compatible 接口
- 支持 `v1/chat/completions`、`v1/responses` 和自动切换
- 支持 PubMed 检索、参考文献编号插入和 RIS 导出
- 支持用户注册、邮箱验证码、配额控制和任务进度显示
- 支持直接部署和 Docker 部署

## 目录

```text
app/services/openai_compat.py      OpenAI-compatible 请求封装
app/services/ncbi.py               PubMed/NCBI 检索
app/services/citation_pipeline.py  引文规划和插入流程
app/services/user_store.py         用户、会话和配额
app/utils/ris.py                   RIS 导出
app/web.py                         HTTP 路由
static/                            前端页面、脚本、样式
server.py                          服务入口
Dockerfile                         Docker 镜像构建
docker-compose.yml                 Docker Compose 部署
deploy/systemd/                    直接部署示例
```

## 直接部署

要求：

- Python 3.12 或兼容版本
- 能访问 OpenAI-compatible 接口和 NCBI

步骤：

1. 克隆仓库。
2. 安装依赖：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

3. 复制配置模板并填写真实值：

```bash
cp auth.example.json auth.json
```

4. 启动服务：

```bash
python3 server.py
```

默认监听 `0.0.0.0:14785`。

浏览器访问：

```text
http://127.0.0.1:14785
```

如果需要 `systemd`，可以参考 [deploy/systemd/addref.service.example](deploy/systemd/addref.service.example)。

## Owner 账号与首次登录

AddRef 不带可直接使用的默认公开账号。

首次启动前，请先编辑 `auth.json`，设置：

- `OWNER_email`
- `OWNER_password`

示例：

```json
{
  "OWNER_email": "admin@example.com",
  "OWNER_password": "请改成强密码"
}
```

规则如下：

- 服务启动时会自动确保 `auth.json` 中配置的 owner 账号存在
- 部署完成后，访问 `/auth`，用 `auth.json` 里的 `OWNER_email` 和 `OWNER_password` 登录
- 普通用户需要通过邮箱验证码自行注册

后续如果要修改 owner 登录信息：

- 修改 `auth.json` 中的 `OWNER_email` 和/或 `OWNER_password`
- 重启服务或容器
- 使用新的 owner 账号信息重新登录

不要直接使用示例配置中的默认值上线。

## Docker 部署

先准备配置文件：

```bash
cp auth.example.json auth.json
mkdir -p data
```

构建并运行：

```bash
docker compose up -d --build
```

停止：

```bash
docker compose down
```

说明：

- 容器对外暴露 `14785`
- `auth.json` 以只读方式挂载到容器
- `data/` 挂载出来用于保存数据库和运行数据

## 许可

本项目公开源码，但不按 OSI 意义上的开源许可证发布。

- 非商业用途适用 [PolyForm Noncommercial 1.0.0](LICENSE)
- 必需版权声明见 [NOTICE](NOTICE)
- 商业使用请联系 `yangzhuangqi@gmail.com`
- 商用授权说明见 [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md)
