# 开发文档 6.2 约定 make dev / test / eval / up；
# 当前骨架阶段只实现已具备的环节，其余随对应模块补充。

PY    := .venv/bin/python
VENV  := .venv
Q     ?= 刻蚀机真空度异常怎么排查？

.PHONY: venv install test ask ask-stream clean

## venv: 创建虚拟环境
venv:
	python3 -m venv $(VENV)

## install: 建虚拟环境并安装锁定版本依赖
install: venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

## test: 跑 LangGraph 链路测试
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
