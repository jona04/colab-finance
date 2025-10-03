.PHONY: build test fmt clean deploy-sepolia verify-sepolia fork-test

build:
	forge build

fmt:
	forge fmt

test:
	forge test -vvv

fork-test:
	forge test -vvv --fork-url $$RPC_BASE_SEPOLIA

deploy-sepolia:
	forge script script/Deploy.s.sol:Deploy \
		--rpc-url $$RPC_BASE_SEPOLIA \
		--broadcast --private-key $$PRIVATE_KEY -vvv

verify-sepolia:
	# Exemplo: substitua ADDRESS pelo address retornado no deploy
	forge verify-contract ADDRESS src/core/SingleUserVault.sol:SingleUserVault \
		--chain 84532 --watch --etherscan-api-key $$ETHERSCAN_API_KEY