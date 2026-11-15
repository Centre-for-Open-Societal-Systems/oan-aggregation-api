// OAN Aggregation API: test, build the image, push it to ECR, package the Helm chart in helm/oan-aggregation-api
// with that image and hand it to oan-helm. The chart is a subchart of openg2p-commons-services (alias
// aggregation-api), bundled there like openg2p-master-data: charts/openg2p-commons-services/charts/<archive>.
// This pipeline does not deploy; the commons-services release in namespace commons runs it once that pull
// request is merged and commons-services is upgraded.
//
// Multibranch job. Every branch and pull request runs the unit tests, builds the image and checks the chart;
// only develop pushes and publishes. Image (the OAN registry naming, eoan/<repository>/<image>):
// eoan/oan-aggregation-api/aggregation-api:<Chart.yaml appVersion>-<branch>.<build number>, e.g.
// 0.0.0-develop.42; the first push creates the ECR repository. The chart of that commit is
// 0.0.0-develop.<commit count> (oan-helm scripts/ci/derive-version.sh). Publishing adds the archive to
// repository/develop/ of oan-helm, then opens a pull request there that bundles it in openg2p-commons-services
// (scripts/ci/vendor_subchart.py; an older open one for this chart is closed as superseded).
//
// Jenkins credentials:
//   aws-ecr-creds            AWS keys with ECR create-repository and push on eoan/oan-aggregation-api/*
//                            (shared with farmer-registry)
//   oan-helm-fork-token      Username + token with push access to oan-helm (shared with farmer-registry)
pipeline {
    agent any
    options {
        skipDefaultCheckout(true)
        disableConcurrentBuilds()
        // Build agents are on-demand spot machines: if one is reclaimed mid-build, run the build again.
        retry(count: 2, conditions: [agent(), nonresumable()])
        timestamps()
        timeout(time: 45, unit: 'MINUTES')
    }
    environment {
        AWS_REGION = 'ap-south-1'
        ECR_REPOSITORY = 'eoan/oan-aggregation-api/aggregation-api'
        CHART = 'oan-aggregation-api'
        // The chart that bundles this one, and where it runs (recorded in the archive's annotations).
        PARENT_CHART = 'openg2p-commons-services'
        HELM_RELEASE = 'commons-services'
        HELM_NAMESPACE = 'commons'
        VALUES_OVERLAY = 'helm/oan-aggregation-api/values-dev.yaml'
        OAN_HELM_FORK_CRED_ID = 'oan-helm-fork-token'
        OAN_HELM = 'https://github.com/Centre-for-Open-Societal-Systems/oan-helm.git'
    }
    stages {
        stage('Build') {
            steps {
                checkout scm
                script {
                    // checkout scm does not always export GIT_COMMIT: resolve the workspace revision.
                    def commit = sh(returnStdout: true, script: 'git rev-parse --verify HEAD').trim()
                    if (!(commit ==~ /[0-9a-f]{40}/)) {
                        error('Could not resolve the checked-out Git commit')
                    }
                    env.SOURCE_COMMIT = commit
                    def appVersion = sh(returnStdout: true,
                                        script: 'sed -n "s/^appVersion: *//p" "helm/$CHART/Chart.yaml" | tr -d \'"\'').trim()
                    if (!(appVersion ==~ /\d+\.\d+\.\d+/)) {
                        error("helm/${env.CHART}/Chart.yaml appVersion '${appVersion}' is not N.N.N")
                    }
                    // <version>-<branch>.<build>: a branch name may hold characters a tag may not.
                    def branch = (env.BRANCH_NAME ?: 'local').toLowerCase().replaceAll(/[^a-z0-9.]+/, '-')
                    env.IMAGE_TAG = "${appVersion}-${branch}.${env.BUILD_NUMBER}"
                    currentBuild.description = "image ${env.IMAGE_TAG}"
                }
                // The image is the test environment: the same framework and dependencies the service runs with.
                sh '''
                    set -eu
                    docker build -f deploy/Dockerfile -t "coss-ci/oan-aggregation-api:$IMAGE_TAG" .
                    docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/repo:ro" -w /repo \
                      "coss-ci/oan-aggregation-api:$IMAGE_TAG" sh -ec '
                        pip install -q --user pytest jsonschema
                        python -m pytest -p no:cacheprovider test/unit'
                '''
            }
        }
        stage('Chart check') {
            steps {
                sh '''
                    set -eu
                    # Helm in a container: the build agent need not have it.
                    helm() { docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/w" -w /w alpine/helm:3.16.4 "$@"; }
                    helm lint "helm/$CHART" -f "$VALUES_OVERLAY" --set image.tag=check
                    helm template "$HELM_RELEASE" "helm/$CHART" -n "$HELM_NAMESPACE" -f "$VALUES_OVERLAY" \
                      --set image.tag=check > /dev/null
                '''
            }
        }
        stage('Push to ECR') {
            when { allOf { branch 'develop'; not { changeRequest() } } }
            steps {
                withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-ecr-creds']]) {
                    script {
                        env.ECR_REGISTRY = sh(returnStdout: true, script: '''
                          set +x
                          account=$(aws sts get-caller-identity --query Account --output text)
                          case "$account" in *[!0-9]*|'') exit 1 ;; esac
                          test "${#account}" = 12
                          printf '%s.dkr.ecr.%s.amazonaws.com' "$account" "$AWS_REGION"
                        ''').trim()
                    }
                    sh '''
                        set -eu
                        set +x
                        export DOCKER_CONFIG
                        DOCKER_CONFIG=$(mktemp -d)
                        trap 'rm -rf "$DOCKER_CONFIG"' EXIT
                        aws ecr get-login-password --region "$AWS_REGION" |
                          docker login --username AWS --password-stdin "$ECR_REGISTRY"
                        # ECR does not create repositories on push: the first build creates it.
                        aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$ECR_REPOSITORY" > /dev/null 2>&1 ||
                          aws ecr create-repository --region "$AWS_REGION" --repository-name "$ECR_REPOSITORY" \
                            --image-scanning-configuration scanOnPush=true > /dev/null
                        docker tag "coss-ci/oan-aggregation-api:$IMAGE_TAG" "$ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG"
                        docker push "$ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG"
                        digest=$(aws ecr describe-images --region "$AWS_REGION" --repository-name "$ECR_REPOSITORY" \
                          --image-ids "imageTag=$IMAGE_TAG" --query 'imageDetails[0].imageDigest' --output text)
                        case "$digest" in sha256:*) ;; *) echo "no digest for $ECR_REPOSITORY:$IMAGE_TAG" >&2; exit 1 ;; esac
                        mkdir -p build-outcomes
                        # oan-helm package_chart.py: values path, repository, tag@digest.
                        printf 'image\\t%s\\t%s@%s\\n' "$ECR_REGISTRY/$ECR_REPOSITORY" "$IMAGE_TAG" "$digest" \
                          > build-outcomes/images.tsv
                        cat build-outcomes/images.tsv
                    '''
                }
                stash name: 'images', includes: 'build-outcomes/images.tsv'
            }
        }
        stage('Release') {
            // The deploy agent has helm, git and python3 for the oan-helm scripts.
            when {
                beforeAgent true
                allOf { branch 'develop'; not { changeRequest() } }
            }
            agent { label 'oan-ci-deploy' }
            stages {
                stage('Package chart') {
                    // The chart with this build's image written into its values: the archive oan-helm gets.
                    steps {
                        checkout scm
                        sh 'rm -rf build-outcomes built-chart .oan-helm'
                        unstash 'images'
                        script {
                            env.CHART_ARCHIVE = sh(returnStdout: true, script: '''
                                set -eu
                                # The chart version is the commit count, so it needs the full history.
                                if [ "$(git rev-parse --is-shallow-repository)" = true ]; then git fetch -q --unshallow; fi
                                git clone -q --depth 1 --filter=blob:none --sparse "$OAN_HELM" .oan-helm
                                git -C .oan-helm sparse-checkout set scripts/ci
                                source_repository=$(git config --get remote.origin.url |
                                  sed -E 's#^(https://[^/]+/|git@[^:]+:)##; s#\\.git$##')
                                python3 .oan-helm/scripts/ci/package_chart.py \
                                  --chart "helm/$CHART" --images build-outcomes/images.tsv --output built-chart \
                                  --source-repository "$source_repository" --source-commit "$SOURCE_COMMIT" \
                                  --subject "$(git log -1 --pretty=%s)" --release "$HELM_RELEASE" \
                                  --namespace "$HELM_NAMESPACE" --overlay "$VALUES_OVERLAY" --branch develop
                            ''').trim().readLines().last()
                        }
                        sh 'helm lint "$CHART_ARCHIVE" -f "$VALUES_OVERLAY"'
                        archiveArtifacts artifacts: 'built-chart/*.tgz,build-outcomes/images.tsv'
                    }
                }
                stage('Publish chart to oan-helm') {
                    // Adds the archive to repository/develop/ of oan-helm (its workflow then updates the index,
                    // CHANGELOG.md and the rollback history), then opens the pull request that bundles it in
                    // openg2p-commons-services. If either fails the build is UNSTABLE: the image is in ECR.
                    steps {
                        catchError(buildResult: 'UNSTABLE', stageResult: 'UNSTABLE') {
                            withCredentials([usernamePassword(credentialsId: env.OAN_HELM_FORK_CRED_ID, usernameVariable: 'GH_USER', passwordVariable: 'GH_TOKEN')]) {
                                sh '''
                                    set +e
                                    # 4 from either: a rerun of this commit, already published or proposed.
                                    python3 .oan-helm/scripts/ci/publish_chart.py "$CHART_ARCHIVE"
                                    status=$?
                                    [ "$status" -eq 0 ] || [ "$status" -eq 4 ] || exit "$status"
                                    python3 .oan-helm/scripts/ci/vendor_subchart.py --parent "$PARENT_CHART" "$CHART_ARCHIVE"
                                    status=$?
                                    [ "$status" -eq 4 ] && exit 0
                                    exit "$status"
                                '''
                            }
                        }
                    }
                }
            }
        }
    }
    post {
        failure {
            script {
                // Mail the DevOps list and the commit author; a controller without SMTP must not hide the failure.
                try {
                    def author = ''
                    try { author = sh(script: "git log -1 --pretty=format:'%ae'", returnStdout: true).trim() } catch (ignored) { author = '' }
                    def recipients = ['simretyibeltal@gmail.com', 'pavanns.ns@gmail.com']
                    if (author && !author.contains('noreply')) { recipients = [author] + recipients }
                    mail(
                        to: recipients.unique().join(', '),
                        subject: "Build FAILED: ${env.JOB_NAME} #${env.BUILD_NUMBER}",
                        body: "OAN Aggregation API build or publish failed.\n\nJob:     ${env.JOB_NAME}\nBranch:  ${env.BRANCH_NAME}\nBuild:   #${env.BUILD_NUMBER}\nURL:     ${env.BUILD_URL}\nConsole: ${env.BUILD_URL}console\n\nJenkins"
                    )
                } catch (err) {
                    echo "notification not sent: ${err.message}"
                }
            }
        }
    }
}
