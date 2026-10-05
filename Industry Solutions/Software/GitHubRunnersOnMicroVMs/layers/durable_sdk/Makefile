# SAM `BuildMethod: makefile` contract: implement build-<LayerLogicalId>.
# SAM sets ARTIFACTS_DIR to the output location; layer code must land under
# python/ so it is importable at /opt/python at runtime.
build-DurableSdkLayer:
	mkdir -p "$(ARTIFACTS_DIR)/python"
	python3 -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)/python"
