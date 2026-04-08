DIST_DIR   := dist
SERVER_DIST := ../static/dist
PYZ_ARM64  := $(DIST_DIR)/frameit-agent-arm64.pyz
PYZ_ARMV7  := $(DIST_DIR)/frameit-agent-armv7.pyz

.PHONY: all build-arm64 build-armv7 install clean

all: build-arm64 build-armv7

# shiv packages agent.py + all dependencies into a self-executing .pyz
# The -c flag sets the entry point (calls agent:main or runs agent.py as __main__)
build-arm64:
	@echo "==> Building arm64 .pyz..."
	mkdir -p $(DIST_DIR)
	pip install shiv --quiet
	shiv -o $(PYZ_ARM64) -c agent -r requirements.txt .
	@echo "    Built $(PYZ_ARM64)"

build-armv7:
	@echo "==> Building armv7 .pyz..."
	mkdir -p $(DIST_DIR)
	pip install shiv --quiet
	shiv -o $(PYZ_ARMV7) -c agent -r requirements.txt .
	@echo "    Built $(PYZ_ARMV7)"

# Copy built .pyz files to the main app's static/dist so Flask can serve them
install: all
	@echo "==> Copying to $(SERVER_DIST)..."
	mkdir -p $(SERVER_DIST)
	cp $(PYZ_ARM64) $(SERVER_DIST)/
	cp $(PYZ_ARMV7) $(SERVER_DIST)/
	@echo "    Done. Binaries available at /static/dist/"

clean:
	rm -rf $(DIST_DIR)
