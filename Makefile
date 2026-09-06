# Build and push the AURUM image to ECR.
#
#   make push
#   make push TAG=a3aff7a
#
# TAG defaults to the current short SHA. It is also injected as AURUM_GIT_SHA on every
# task, so a model version is traceable back to a commit — do not push from a dirty tree
# and expect the stamp to mean anything.

AWS_REGION  ?= us-east-1
ACCOUNT_ID  ?= $(shell aws sts get-caller-identity --query Account --output text)
ECR_REPO    ?= aurum
ECR_URI     := $(ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com/$(ECR_REPO)
TAG         ?= $(shell git rev-parse --short HEAD)
DOCKERFILE  := docker/aurum.Dockerfile

.PHONY: push build login print-tag

# --platform linux/amd64 is not optional on Apple silicon: Fargate refuses an arm64 image.
build:
	docker build --platform linux/amd64 -f $(DOCKERFILE) -t $(ECR_REPO):$(TAG) .

login:
	aws ecr get-login-password --region $(AWS_REGION) \
	  | docker login --username AWS --password-stdin $(ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com

push: build login
	docker tag $(ECR_REPO):$(TAG) $(ECR_URI):$(TAG)
	docker push $(ECR_URI):$(TAG)
	@echo
	@echo "Pushed $(ECR_URI):$(TAG)"
	@echo "Apply with: terraform apply -var=\"image_tag=$(TAG)\""

print-tag:
	@echo $(TAG)
