// Build the aggregation API image and push it to ECR.
//
// The image is not deployed from here: the Commons release pins it by digest in oan-helm
// (aggregationApi.image), so a deploy is a reviewed values change. This job produces the
// immutable dev-<commit12> tag, the digest to pin, and on develop the moving `develop` alias.
pipeline {
    agent { label 'vpn-agent2' }

    options {
        disableConcurrentBuilds()
    }

    environment {
        AWS_ACCOUNT_ID = "379220350808"
        AWS_REGION     = "ap-south-1"
        ECR_REGISTRY   = "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
        ECR_REPOSITORY = "oan/commons/aggregation-api"
    }

    stages {
        stage('Checkout') {
            steps { checkout scm }
        }

        stage('Check') {
            steps {
                sh '''
                    set -eu
                    bash -n ci/build-push.sh
                    python3 -m compileall -q backend/src
                '''
            }
        }

        stage('ECR Login') {
            steps {
                withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-ecr-creds']]) {
                    sh "aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ECR_REGISTRY}"
                }
            }
        }

        stage('Build & Push') {
            // dev-<commit12> is immutable. develop also moves the `develop` alias (skipped when
            // the repository has immutable tags); a git tag build pushes the tag itself, the
            // release version staging pins.
            steps {
                script {
                    env.IMAGE_TAG = env.GIT_COMMIT.take(12)
                    def extra = env.TAG_NAME ?: (env.BRANCH_NAME == 'develop' ? 'develop' : '-')
                    sh 'rm -rf .build && mkdir -p .build'
                    sh "ci/build-push.sh aggregation-api ${ECR_REPOSITORY} deploy/Dockerfile . ${env.IMAGE_TAG} ${extra} no"
                }
            }
            post {
                success {
                    archiveArtifacts artifacts: '.build/digest-*', allowEmptyArchive: false
                }
            }
        }
    }

    post {
        failure {
            echo "Aggregation API ${env.BRANCH_NAME} build failed."
        }
        always {
            sh 'docker image prune -f || true'
            sh "docker logout ${ECR_REGISTRY} || true"
        }
    }
}
