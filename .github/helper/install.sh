#!/bin/bash

set -e

cd ~ || exit

echo "::group::Install Dependencies"
sudo apt update
sudo apt remove mysql-server mysql-client
sudo apt install libcups2-dev redis-server mariadb-client
echo "::endgroup::"

echo "::group::Install Bench"
pip install frappe-bench
echo "::endgroup::"

echo "::group::Init Bench"
git clone https://github.com/frappe/frappe --branch "$BRANCH_TO_CLONE" --depth 1
bench init --skip-assets --frappe-path ~/frappe --python "$(which python)" frappe-bench
echo "::endgroup::"

echo "::group::Create Test Site"
mkdir ~/frappe-bench/sites/test_site
cp -r "${GITHUB_WORKSPACE}/.github/helper/site_config.json" ~/frappe-bench/sites/test_site/

mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "SET GLOBAL character_set_server = 'utf8mb4'"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "SET GLOBAL collation_server = 'utf8mb4_unicode_ci'"

mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "CREATE USER 'test_frappe'@'localhost' IDENTIFIED BY 'test_frappe'"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "CREATE DATABASE test_frappe"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "GRANT ALL PRIVILEGES ON \`test_frappe\`.* TO 'test_frappe'@'localhost'"

mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "FLUSH PRIVILEGES"
echo "::endgroup::"

cd ~/frappe-bench || exit

echo "::group::Modify Processes"
sed -i 's/^watch:/# watch:/g' Procfile
sed -i 's/^schedule:/# schedule:/g' Procfile
sed -i 's/^socketio:/# socketio:/g' Procfile
sed -i 's/^redis_socketio:/# redis_socketio:/g' Procfile
echo "::endgroup::"

echo "::group::Install Apps"
bench get-app payments --branch "$BRANCH_TO_CLONE"
bench get-app erpnext --branch "$BRANCH_TO_CLONE"
bench get-app healthcare --branch "$BRANCH_TO_CLONE"
bench get-app landms "${GITHUB_WORKSPACE}"

bench setup requirements --dev

# setuptools is required by the dropbox package (frappe dependency)
# which imports pkg_resources. Python 3.11+ venvs no longer include
# setuptools by default, so we install it explicitly.
~/frappe-bench/env/bin/pip install setuptools
echo "::endgroup::"

echo "::group::Build & Install Site"
CI=Yes bench build --app frappe &
build_pid=$!

bench start &> ~/frappe-bench/bench_start.log &

bench --site test_site reinstall --yes

bench --verbose --site test_site install-app landms

# wait till assets are built successfully
wait $build_pid
echo "::endgroup::"
