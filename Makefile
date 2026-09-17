# 开发文档 6.2 约定 make dev / test / eval / up
# 当前已实现：install / dev / dev-web / test / ask / ask-stream / ingest / clean

PY    := .venv/bin/python
VENV  := .venv
Q     ?= 刻蚀机真空度异常怎么排查？
HOST  ?= 127.0.0.1
PORT  ?= 8000

.PHONY: venv install install-web test ask ask-stream ingest dev dev-web clean

## venv: 创建虚拟环境
venv:
	python3 -m venv $(VENV)

## install: 建虚拟环境并安装锁定版本依赖
install: venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

## install-web: 安装前端依赖（Vue3 + Vite）
install-web:
	cd frontend && npm install

## ingest: 一键入库（解析 -> 切片 -> 向量化），--rebuild 先清空索引
ingest:
	$(PY) scripts/ingest.py --rebuild

## dev: 启动后端 API（默认 http://127.0.0.1:8000 ，文档 /docs）
dev:
	$(PY) -m uvicorn backend.main:app --reload --host $(HOST) --port $(PORT)

## dev-web: 启动前端开发服务器（默认 http://127.0.0.1:5173，代理 /api 与 /static 到 8000）
dev-web:
	cd frontend && npm run dev

## test: 跑全部测试（RAG + 链路 + 校验）
test:
	$(PY) -m pytest tests/ -q

## ask: 命令行单问，例如 make ask Q="参数下限是多少"
ask:
	$(PY) scripts/ask.py "$(Q)"

## ask-stream: 走流式链路
ask-stream:
	$(PY) scripts/ask.py "$(Q)" --stream

## clean: 清理字节码与测试缓存
clean:
	find . -name __pycache__ -type d -not -path "./$(VENV)/*" -exec rm -rf {} +
	rm -rf .pytest_cache
