PYTHON ?= python3
VENV ?= .venv
RUFF ?= ruff
INTEGRATION ?= shell
UBUNTU_SNAPSHOT ?= $(LINUX_TOOLCHAIN_UBUNTU_SNAPSHOT)
JOBS ?= $(shell \
	cpu_count=$$(getconf _NPROCESSORS_ONLN); \
	jobs=$$((cpu_count / 4)); \
	if [ "$$jobs" -lt 1 ]; then jobs=1; fi; \
	printf '%s' "$$jobs")
NATIVE_ARCH = $(shell uname -m | sed -e 's/^amd64$$/x86_64/' -e 's/^arm64$$/aarch64/')
TARGET_ARCH = $(if $(strip $(ARCH)),$(strip $(ARCH)),$(NATIVE_ARCH))
COMPILER_NAME = $(subst @,,$(strip $(COMPILER)))
GLIBC_NAME = $(subst .,,$(strip $(GLIBC)))
RUNTIME_SUFFIX = $(if $(strip $(RUNTIME)),-$(subst @,,$(strip $(RUNTIME))))
INTEGRATION_SUFFIX = $(if $(filter-out shell,$(strip $(INTEGRATION))),-$(strip $(INTEGRATION)))
TOOLCHAIN_BASE = $(COMPILER_NAME)-glibc$(GLIBC_NAME)-$(TARGET_ARCH)
TOOLCHAIN_VARIANT = $(TOOLCHAIN_BASE)$(RUNTIME_SUFFIX)$(INTEGRATION_SUFFIX)
PREFIX ?= $(if $(strip $(HOME)),$(HOME)/.local/lib/linux-toolchain/$(TOOLCHAIN_VARIANT))
WORK_DIR ?= $(CURDIR)/out/work/$(TOOLCHAIN_VARIANT)
STORE_DIR ?= $(CURDIR)/out/store
BUNDLE_OUTPUT ?= out/linux-toolchain-$(TOOLCHAIN_VARIANT).run
STORE_ARGUMENT = $(if $(strip $(STORE_DIR)),--store-dir "$(STORE_DIR)")
BUILDER_ENVIRONMENT = LINUX_TOOLCHAIN_UBUNTU_SNAPSHOT="$(strip $(UBUNTU_SNAPSHOT))"
SETUP_ARGUMENTS = \
	$(if $(strip $(ARCH)),--arch "$(ARCH)") \
	$(if $(strip $(RUNTIME)),--runtime "$(RUNTIME)") \
	$(if $(strip $(RUNNER)),--runner "$(RUNNER)") \
	$(SETUP_OPTIONS)
SETUP_BASE_COMMAND = \
	$(BUILDER_ENVIRONMENT) \
	"$(VENV)/bin/linux-toolchain" setup "$(COMPILER)" \
		--glibc "$(GLIBC)" \
		--integration "$(INTEGRATION)" \
		--jobs "$(JOBS)" \
		--work-dir "$(WORK_DIR)" \
		$(STORE_ARGUMENT) $(SETUP_ARGUMENTS)
SETUP_COMMAND = $(SETUP_BASE_COMMAND) --prefix "$(PREFIX)"

.DEFAULT_GOAL := check

.PHONY: _clean bootstrap bundle check clean compile lint purge python-dist setup test

DIST_DIR ?= dist

bootstrap:
	@set -eu; \
	color=0; \
	if [ -t 2 ] && [ -z "$${NO_COLOR+x}" ] && [ "$${TERM:-}" != dumb ]; then \
		color=1; \
	fi; \
	case "$(VENV)" in "") \
		echo "VENV must name a dedicated virtual-environment directory" >&2; \
		exit 2;; \
	esac; \
	repository_path="$$(pwd -P)"; \
	venv_path="$$(PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).resolve())' "$(VENV)")"; \
	if [ "$$venv_path" = / ] || [ "$$venv_path" = "$$repository_path" ]; then \
		echo "VENV must name a dedicated virtual-environment directory" >&2; \
		exit 2; \
	fi; \
	if [ -e "$(VENV)" ] && [ ! -d "$(VENV)" ]; then \
		echo "VENV must name a directory: $(VENV)" >&2; \
		exit 2; \
	fi; \
	if [ -d "$(VENV)" ] && [ ! -f "$(VENV)/pyvenv.cfg" ] && \
	   [ -n "$$(find "$(VENV)" -mindepth 1 -print -quit)" ]; then \
		echo "VENV is a non-empty directory that is not a virtual environment: $(VENV)" >&2; \
		exit 2; \
	fi; \
	if [ -f "$(VENV)/pyvenv.cfg" ] && [ ! -x "$(VENV)/bin/python" ]; then \
		echo "VENV is incomplete: $(VENV)" >&2; \
		exit 2; \
	fi; \
	requested_python="$$(PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"; \
	if [ -x "$(VENV)/bin/python" ]; then \
		venv_python="$$(PYTHONDONTWRITEBYTECODE=1 "$(VENV)/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"; \
		if [ "$$venv_python" != "$$requested_python" ]; then \
			echo "VENV uses Python $$venv_python, but PYTHON requests $$requested_python; choose another VENV" >&2; \
			exit 2; \
		fi; \
	fi; \
	stamp="$(VENV)/.linux-toolchain-bootstrap"; \
	fingerprint="$$(PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -c 'import hashlib, pathlib, sys; data = pathlib.Path("pyproject.toml").read_bytes() + b"\0" + sys.argv[1].encode() + b"\0" + str(sys.version_info[:2]).encode(); print(hashlib.sha256(data).hexdigest())' "$$repository_path")"; \
	current=""; \
	if [ -f "$$stamp" ]; then \
		IFS= read -r current < "$$stamp" || true; \
	fi; \
	if [ "$$current" = "$$fingerprint" ] && \
	   [ -x "$(VENV)/bin/linux-toolchain" ] && [ -x "$(VENV)/bin/ruff" ]; then \
		if [ "$$color" -eq 1 ]; then \
			printf '\033[1;36m==>\033[0m \033[1mbootstrap:\033[0m %s \033[1;32mREADY\033[0m\n' "$(VENV)" >&2; \
		else \
			printf '==> bootstrap: %s READY\n' "$(VENV)" >&2; \
		fi; \
		exit 0; \
	fi; \
	if [ "$$color" -eq 1 ]; then \
		printf '\033[1;36m==>\033[0m \033[1mbootstrap:\033[0m preparing %s ... ' "$(VENV)" >&2; \
	else \
		printf '==> bootstrap: preparing %s ... ' "$(VENV)" >&2; \
	fi; \
	if [ ! -x "$(VENV)/bin/python" ]; then $(PYTHON) -m venv "$(VENV)"; fi; \
	"$(VENV)/bin/python" -m pip install --quiet --disable-pip-version-check \
		-e '.[dev]'; \
	temporary_stamp="$$stamp.tmp"; \
	printf '%s\n' "$$fingerprint" > "$$temporary_stamp"; \
	mv -f -- "$$temporary_stamp" "$$stamp"; \
	if [ "$$color" -eq 1 ]; then \
		printf '\033[1;32mDONE\033[0m\n' >&2; \
	else \
		printf 'DONE\n' >&2; \
	fi

setup:
	@if [ -z "$(strip $(COMPILER))" ] || [ -z "$(strip $(GLIBC))" ] || \
	   [ -z "$(strip $(PREFIX))" ] || [ -z "$(strip $(WORK_DIR))" ]; then \
		echo 'usage: make setup COMPILER=gcc@12 GLIBC=2.19 [PREFIX=$$HOME/.local/lib/linux-toolchain/gcc12-glibc219-x86_64] [WORK_DIR=$$PWD/out/work/gcc12-glibc219-x86_64] [STORE_DIR=/shared/linux-toolchain/store] [UBUNTU_SNAPSHOT=YYYYMMDDTHHMMSSZ]' >&2; \
		exit 2; \
	fi
	@$(MAKE) --no-print-directory bootstrap
	@$(SETUP_COMMAND) >/dev/null

bundle:
	@if [ -z "$(strip $(COMPILER))" ] || [ -z "$(strip $(GLIBC))" ] || \
	   [ -z "$(strip $(WORK_DIR))" ] || \
	   [ -z "$(strip $(BUNDLE_OUTPUT))" ]; then \
		echo 'usage: make bundle COMPILER=gcc@12 GLIBC=2.19 [WORK_DIR=$$PWD/out/work/gcc12-glibc219-x86_64] [STORE_DIR=/shared/linux-toolchain/store] [BUNDLE_OUTPUT=out/linux-toolchain-gcc12-glibc219-x86_64.run] [UBUNTU_SNAPSHOT=YYYYMMDDTHHMMSSZ]' >&2; \
		exit 2; \
	fi
	@$(MAKE) --no-print-directory bootstrap
	@$(SETUP_BASE_COMMAND) --prepare-only >/dev/null
	@$(BUILDER_ENVIRONMENT) "$(VENV)/bin/linux-toolchain" bundle create \
		--config "$(WORK_DIR)/setup.json" \
		--state-directory "$(WORK_DIR)/state" \
		--output "$(BUNDLE_OUTPUT)" $(BUNDLE_OPTIONS)

check: compile test

_clean:
	@set -eu; \
	if [ -L out ]; then \
		echo 'clean refuses to traverse a symlinked out directory' >&2; \
		exit 2; \
	fi; \
	if [ -d out/work ] && [ ! -L out/work ]; then \
		find out/work -type d -exec chmod u+w -- {} +; \
	fi; \
	rm -rf -- build dist out/work .cache .pytest_cache .ruff_cache; \
	rm -f -- .coverage; \
	find src -maxdepth 1 -type d -name '*.egg-info' -prune -exec rm -rf -- {} +; \
	find src tests -type d -name __pycache__ -prune -exec rm -rf -- {} +; \
	if [ -d out ]; then \
		find out -maxdepth 1 -type f -name '*.run' -delete; \
	fi

clean: _clean
	@if [ -t 2 ] && [ -z "$${NO_COLOR+x}" ] && [ "$${TERM:-}" != dumb ]; then \
		printf '\033[1;36m==>\033[0m \033[1mclean:\033[0m \033[1;32mDONE\033[0m\n' >&2; \
	else \
		printf '==> clean: DONE\n' >&2; \
	fi

purge: _clean
	@set -eu; \
	for path in .venv out; do \
		if [ -d "$$path" ] && [ ! -L "$$path" ]; then \
			find "$$path" -type d -exec chmod u+w -- {} +; \
		fi; \
	done; \
	rm -rf -- .venv out
	@if [ -t 2 ] && [ -z "$${NO_COLOR+x}" ] && [ "$${TERM:-}" != dumb ]; then \
		printf '\033[1;36m==>\033[0m \033[1mpurge:\033[0m \033[1;32mDONE\033[0m\n' >&2; \
	else \
		printf '==> purge: DONE\n' >&2; \
	fi

lint:
	@set -eu; \
	ruff="$(RUFF)"; \
	if [ -x "$(VENV)/bin/ruff" ]; then ruff="$(VENV)/bin/ruff"; fi; \
	"$$ruff" check src tests; \
	"$$ruff" format --check src tests

compile:
	@cache_dir="$$(mktemp -d)"; \
	trap 'rm -rf "$$cache_dir"' EXIT; \
	PYTHONPYCACHEPREFIX="$$cache_dir" PYTHONPATH=src $(PYTHON) -m compileall -q src tests

test:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src $(PYTHON) -m unittest discover -s tests

python-dist:
	@set -eu; \
	umask 022; \
	python="$(PYTHON)"; \
	if [ -x "$(VENV)/bin/python" ]; then python="$(VENV)/bin/python"; fi; \
	if [ -L "$(DIST_DIR)" ] || { [ -e "$(DIST_DIR)" ] && [ ! -d "$(DIST_DIR)" ]; }; then \
		echo "DIST_DIR must be a directory, not a file or symlink: $(DIST_DIR)" >&2; \
		exit 2; \
	fi; \
	if [ -d "$(DIST_DIR)" ] && [ -n "$$(find "$(DIST_DIR)" -mindepth 1 -print -quit)" ]; then \
		echo "DIST_DIR must be empty to avoid mixing release artifacts: $(DIST_DIR)" >&2; \
		exit 2; \
	fi; \
	cleanup() { \
		rm -rf build src/*.egg-info; \
		find src tests -type d -name __pycache__ -prune -exec rm -rf {} +; \
	}; \
	trap cleanup EXIT HUP INT TERM; \
	mkdir -p "$(DIST_DIR)"; \
	PIP_NO_CACHE_DIR=1 PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
		"$$python" -m pip wheel --no-deps --no-build-isolation \
		--wheel-dir "$(DIST_DIR)" .; \
	"$$python" -c 'from setuptools.build_meta import build_sdist; build_sdist("$(DIST_DIR)")'; \
	sdist="$$(find "$(DIST_DIR)" -maxdepth 1 -type f -name '*.tar.gz' -print -quit)"; \
	if [ -z "$$sdist" ]; then \
		echo "source distribution was not created in $(DIST_DIR)" >&2; \
		exit 2; \
	fi; \
	chmod 0644 "$(DIST_DIR)"/*.whl "$$sdist"
