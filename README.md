Environment variables for the Docker image:
    - `TB_BOARD`: mandatory board name to build
    - `TB_REPO`: the git repository, e.g. `https://github.com/ccrisan/thingos.git`; mandatory, unless a local build is being done
    - `TB_GIT_CREDENTIALS`: optional git credentials, e.g. `username:password`
    - `TB_BRANCH`: an optional git branch to checkout
    - `TB_TAG`: an optional git tag to checkout
    - `TB_COMMIT`: an optional git commit to checkout
    - `TB_PR`: an optional git PR to checkout
    - `TB_VERSION`: OS image version to set
    - `TB_CUSTOM_CMD`: a custom command to execute instead of the build command
    - `TB_CLEAN_TARGET_ONLY`: set to `true` to simply do `clean-target` instead of running `distclean`

To build locally:

    docker run -it -v /path/to/thingos:/os -e TB_BOARD=your_board ccrisan/thingos-builder

