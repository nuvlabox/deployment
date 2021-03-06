#!/usr/bin/env python3

import cleanup
import atexit
import time
import signal
import json
import docker
import pytest
import uuid
import requests
import nuvla
import logging
import os
from tempfile import NamedTemporaryFile
from contextlib import contextmanager
from git import Repo
from nuvla.api import Api


api = Api(endpoint="https://nuvla.io",
          reauthenticate=True)
docker_client = docker.from_env()

cert = NamedTemporaryFile()
key = NamedTemporaryFile()

cleaner = cleanup.Cleanup(api, docker_client)
atexit.register(cleaner.goodbye)

repo = Repo("..")
if repo.active_branch.name == "master":
    nbe_installer_image = "nuvlabox/installer:master"
else:
    nbe_installer_image = f"nuvladev/installer:{repo.active_branch.name}"


@contextmanager
def timeout(deadline):
    # Register a function to raise a TimeoutError on the signal.
    signal.signal(signal.SIGALRM, raise_timeout)
    # Schedule the signal to be sent after ``time``.
    signal.alarm(deadline)

    try:
        yield
    except TimeoutError:
        raise Exception(f'Exceeded the timeout of {deadline} sec while waiting for NuvlaBox to be COMMISSIONED')
    finally:
        # Unregister the signal so it won't be triggered
        # if the timeout is not reached.
        signal.signal(signal.SIGALRM, signal.SIG_IGN)


def raise_timeout(signum, frame):
    raise TimeoutError


# THESE ARE STATIC AND ASSUMED TO BE IN NUVLA
credential_id = "credential/78bdb494-4d8d-457a-a484-a582719ab32c"
install_module_id = "module/787510b0-9d9e-49d5-98c6-ac96cfc38301"


def test_nuvla_login():
    logging.info('Fetching Nuvla API key credential from environment...')
    apikey = os.getenv('NUVLA_DEV_APIKEY')
    _apisecret = os.getenv('NUVLA_DEV_APISECRET')
    assert apikey.startswith("credential/"), "A valid Nuvla API key needs to start with 'credential/'"

    logging.info(f'Authenticating with Nuvla at {api.endpoint}')
    api.login_apikey(apikey, _apisecret)
    assert api.is_authenticated(), "The provided Nuvla API key credential is not valid"


def test_zero_nuvlaboxes(remote):
    logging.info('We shall not run this test if there are leftover NuvlaBox resources in Nuvla...')
    existing_nuvlaboxes = api.get('nuvlabox',
                                  filter='description^="NuvlaBox for E2E testing - commit" and state="COMMISSIONED"')
    # if there are NBs then it means a previous test run left uncleaned resources. This must be fixed manually
    assert existing_nuvlaboxes.data.get('count', 0) == 0, 'There are leftovers from previous tests'

    if remote:
        dep_filter = f'module/id="{install_module_id}" and state!="STOPPED"'
        existing_nuvlabox_deployments = api.get('deployment', filter=dep_filter)

        # if there are active NB deployments then it means a previous test run left uncleaned resources
        # This must be fixed manually
        assert existing_nuvlabox_deployments.data.get('count', 0) == 0, \
            f'There are {existing_nuvlabox_deployments.data.get("count")} leftover deployments from previous tests'


def get_nuvlabox_version():
    major = 1
    for tag in repo.tags:
        major = int(tag.name.split('.')[0]) if int(tag.name.split('.')[0]) > major else major

    return major


def create_nuvlabox_body(vpn_server_id, local_nuvlabox, body=None):
    name = "(local) Test NuvlaBox" if local_nuvlabox else "(CI) Test NuvlaBox"

    if not body:
        body = {
            "name": name,
            "description": f"NuvlaBox for E2E testing - commit {repo.head.commit.hexsha}, by {repo.head.commit.author}",
            "version": get_nuvlabox_version()
        }

        if vpn_server_id:
            body["vpn-server-id"] = vpn_server_id

    return body


def test_create_new_nuvlaboxes(request, remote, vpnserver):
    if remote:
        new_nb = api.add('nuvlabox', data=create_nuvlabox_body(vpnserver, False))
        nuvlabox_id = new_nb.data.get('resource-id')

        assert nuvlabox_id is not None, f'Failed to create NuvlaBox resource in {api.endpoint}'
        assert new_nb.data.get('status', -1) == 201, f'Failed to create NuvlaBox resource in {api.endpoint}'

        logging.info(f'Created new NuvlaBox (remote) with UUID {nuvlabox_id}')
        request.config.cache.set('nuvlabox_id_remote', nuvlabox_id)

        atexit.register(cleaner.delete_nuvlabox, nuvlabox_id)

    new_nb = api.add('nuvlabox', data=create_nuvlabox_body(vpnserver, True))
    nuvlabox_id = new_nb.data.get('resource-id')

    assert nuvlabox_id is not None, f'Failed to create NuvlaBox (local) resource in {api.endpoint}'
    assert new_nb.data.get('status', -1) == 201, f'Failed to create NuvlaBox (local) resource in {api.endpoint}'

    logging.info(f'Created new NuvlaBox (local) with UUID {nuvlabox_id}')
    request.config.cache.set('nuvlabox_id_local', nuvlabox_id)

    atexit.register(cleaner.delete_nuvlabox, nuvlabox_id)


def test_deploy_nuvlaboxes(request, remote):
    # This app is in Nuvla.io and has been created by dev@sixsq.com, as group/sixsq-devs
    if remote:
        # deploy NB via Nuvla.io
        logging.info(f'Deploying NuvlaBox Engine remotely via {api.endpoint}')
        # find module
        nuvlabox_id_remote = request.config.cache.get('nuvlabox_id_remote', '')
        assert nuvlabox_id_remote != '', 'PyTest cache is not working'
        nuvlabox_engine_app = api.get(install_module_id)

        assert nuvlabox_engine_app.id == install_module_id, f'Sanity check for Nuvla app ID {install_module_id} failed'

        create_deployment_body = {
            "module": {
                "href": install_module_id
            }
        }

        # create app deployment
        create_deployment = api.add('deployment', data=create_deployment_body)
        assert create_deployment.data.get('status', -1) == 201, f'NuvlaBox deployment failed: {create_deployment.data}'

        deployment_id = create_deployment.data.get('resource-id')
        request.config.cache.set('deployment_id', deployment_id)
        atexit.register(cleaner.delete_install_deployment, deployment_id)

        deployment = api.get(deployment_id)
        deployment.data['module']['content']['environmental-variables'][0].update({"value": nuvlabox_id_remote})

        deployment.data['parent'] = credential_id

        # add credential to deployment
        install_deployment_edited = api.edit(deployment_id, data=deployment.data)
        assert install_deployment_edited.data.get('parent') == credential_id, \
            f'Failed to add {credential_id} to {deployment_id}'
        nb_uuid_env_var = install_deployment_edited.data['module']['content']['environmental-variables'][0]
        assert nb_uuid_env_var['value'] == nuvlabox_id_remote, f'Failed to add env vars to {deployment_id}'

        # start deployment
        api.get(deployment_id + "/start")
        # atexit.unregister(cleaner.delete_install_deployment)
        atexit.register(cleaner.stop_install_deployment, deployment_id)

        logging.info(f'Started remote NuvlaBox Engine from Nuvla deployment {deployment_id}')

    nuvlabox_id_local = request.config.cache.get('nuvlabox_id_local', '')
    assert nuvlabox_id_local != '', 'PyTest cache is not working'

    # deploy local NB
    docker_client.images.pull(nbe_installer_image)
    local_project_name = str(uuid.uuid4())
    request.config.cache.set('local_project_name', local_project_name)
    try:
        docker_client.containers.run(nbe_installer_image,
                                     environment=[f"NUVLABOX_UUID={nuvlabox_id_local}",
                                                  f"HOME={os.getenv('HOME')}"],
                                     command=f"install --project={local_project_name}",
                                     name="nuvlabox-engine-installer",
                                     volumes={
                                         '/var/run/docker.sock': {'bind': '/var/run/docker.sock',
                                                                  'mode': 'ro'}
                                     })
    except docker.errors.ContainerError as e:
        logging.error(f'Cannot install local NuvlaBox Engine. Reason: {str(e)}')

    atexit.register(cleaner.remove_local_nuvlabox, local_project_name, nbe_installer_image)
    # atexit.register(cleaner.delete_nuvlabox, nuvlabox_id_local)
    installer_container = docker_client.containers.get("nuvlabox-engine-installer")
    assert installer_container.attrs['State']['ExitCode'] == 0, 'NBE installer failed'
    logging.info(f'NuvlaBox ({nuvlabox_id_local}) Engine successfully installed with project name {local_project_name}')
    atexit.register(cleaner.decommission_nuvlabox, nuvlabox_id_local)


def test_nuvlabox_engine_containers_stability(request, remote, vpnserver, nolinux):
    nb_containers = docker_client.containers.list(filters={'label': 'nuvlabox.component=True'}, all=True)

    container_names = []
    image_names = []
    for container in nb_containers:
        image_names.append(container.attrs['Config']['Image'])
        if container.name == 'vpn-client' and not vpnserver:
            continue

        if nolinux:
            if (container.attrs['RestartCount'] > 0 or container.status.lower() not in ['running', 'paused']) and \
                    "peripheral-manager-" in container.name:
                logging.warning(f'--no-linux enabled: ignoring RestartCount ({container.attrs["RestartCount"]}) '
                                f'and container state ({container.status}) for {container.name}')
                continue

        assert container.attrs['RestartCount'] == 0, \
            f'Local NuvlaBox container {container.name} is unstable: {json.dumps(container.attrs, indent=2)}'

        # we allow for containers to have exited, provided they have exited without an error
        # like this we cover scenarios where, for example, a peripheral manager is not supported by the testing env
        if container.status.lower() not in ['running', 'paused'] and container.attrs['State']['ExitCode'] == 0:
            logging.warning(f'Container {container.name} is "{container.status}" but with exit code 0. Ignoring it ...')
            continue

        assert container.status.lower() in ['running', 'paused'], \
            f'Local NuvlaBox container {container.name} is not running. Logs are: {container.logs()}. ' \
                f'Details: {json.dumps(container.attrs, indent=2)}'

        container_names.append(container.name)

    logging.info('All NuvlaBox containers from local installation are stable')
    request.config.cache.set('containers', container_names)
    request.config.cache.set('images', image_names)

    if remote:
        deployment_id = request.config.cache.get('deployment_id', '')
        nuvlabox_id = request.config.cache.get('nuvlabox_id_remote', '')

        wait_for_start_job = 0
        while True:
            # deployment is not moving away from created...might be stuck. Gave it 2 attempts already. Aborting
            assert wait_for_start_job < 2, f'Deployment {deployment_id} still in "created" state...might be stuck'

            deployment = api.get(deployment_id)
            current_state = deployment.data['state'].lower()
            if current_state == "starting":
                time.sleep(5)
                continue
            elif current_state == "created":
                logging.warning(f'Remote NuvlaBox deployment {deployment_id} is still in "created" state...')
                wait_for_start_job += 1
                time.sleep(2)
                continue
            elif current_state == "started":
                break
            else:
                assert current_state in ['starting', 'created', 'started'], f'Deployment {deployment_id} failed'

        logging.info(f'Remote NuvlaBox started from deployment {deployment_id} is now up and running')

        # wait 60 seconds for activation
        decommission_registered = 0
        with timeout(60):
            while True:
                # now check for the NuvlaBox
                nuvlabox = api.get(nuvlabox_id)

                nb_state = nuvlabox.data['state'].lower()
                if nb_state == "commissioned":
                    # just to be sure, give time for a 2nd telemetry cycle to arrive
                    time.sleep(30)
                    break
                elif nb_state == "activated" and decommission_registered == 0:
                    # in case the commissioning never comes
                    logging.warning(f'NuvlaBox {nuvlabox_id} activated but still not commissioned...')
                    atexit.register(cleaner.decommission_nuvlabox, nuvlabox_id)
                    decommission_registered = 1
                else:
                    time.sleep(3)

        # In case the activation went too fast
        if decommission_registered == 0:
            atexit.register(cleaner.decommission_nuvlabox, nuvlabox_id)

        # atexit.unregister(cleaner.delete_nuvlabox)

        assert nb_state == "commissioned", f'NuvlaBox {nuvlabox_id} failed to commission'
        logging.info(f'Remote NuvlaBox {nuvlabox_id} is now commissioned')

        # check for an hearbeat
        nuvlabox = api.get(nuvlabox_id)
        nuvlabox_status_id = nuvlabox.data.get('nuvlabox-status')
        assert nuvlabox_status_id is not None, f'NuvlaBox {nuvlabox_id} is commissioned but not operational'
        request.config.cache.set('nuvlabox_status_id_remote', nuvlabox_status_id)

        nuvlabox_status = api.get(nuvlabox_status_id)
        assert nuvlabox_status.data.get('next-heartbeat') is not None, \
            f'NuvlaBox telemetry not working for {nuvlabox_id}'

        # check operational status
        assert nuvlabox_status.data.get('status', 'OFF').lower() == "operational", \
            f'NuvlaBox {nuvlabox_id} not operational'

        # check that resource consumption is being sent
        assert isinstance(nuvlabox_status.data.get('resources'), dict), \
            f'NuvlaBox {nuvlabox_id} is missing the information about resource consumption'
        assert "cpu" in nuvlabox_status.data.get('resources', {}).keys(), \
            f'NuvlaBox {nuvlabox_id} is missing the information about CPU consumption'

        # check NB API endpoint
        nuvlabox_status = api.get(nuvlabox_status_id)
        assert isinstance(nuvlabox_status.data.get('nuvlabox-api-endpoint'), str), \
            f'Missing management API endpoint attribute for NuvlaBox {nuvlabox_id}'

        logging.info(f'Remote NuvlaBox {nuvlabox_id} has an healthy status')

    nuvlabox_id = request.config.cache.get('nuvlabox_id_local', '')
    # check PULL capability
    with timeout(60):
        while True:
            nuvlabox = api.get(nuvlabox_id)
            if 'capabilities' in nuvlabox.data:
                break

            time.sleep(3)

    assert 'NUVLA_JOB_PULL' in nuvlabox.data.get('capabilities', []), \
        f'NuvlaBox {nuvlabox_id} is missing NUVLA_JOB_PULL capability'


def test_nuvlabox_engine_local_agent_api(request):
    nuvlabox_id_local = request.config.cache.get('nuvlabox_id_local', '')

    agent_api = 'http://localhost:5080/api/'
    r = requests.get(agent_api + 'healthcheck')
    assert r.status_code == 200, f'{agent_api} is not up'
    assert 'true' in r.text.lower(), f'{agent_api} is unhealthy'
    logging.info(f'Agent API ({agent_api}) is healthy')

    # send commissioning
    commission = {
        'tags': ['TEST_TAG']
    }
    r = requests.post(agent_api + 'commission', json=commission)
    assert r.status_code == 200, f'Commissioning {commission} via {agent_api}, has failed'
    logging.info(f'NuvlaBox commissioning is working (tested with payload: {commission})')

    nuvlabox = api.get(nuvlabox_id_local)
    assert 'TEST_TAG' in nuvlabox.data.get('tags', []), f'Commissioning false positive. Tags were not propagated'
    request.config.cache.set('nuvlabox_id_local_isg', nuvlabox.data['infrastructure-service-group'])

    # check peripherals api
    mock_peripheral_identifier = 'mock:usb:peripheral'
    mock_peripheral = {
        'identifier': mock_peripheral_identifier,
        'interface': 'USB',
        'version': get_nuvlabox_version(),
        'name': '(local NB) Mock USB Peripheral',
        'available': True,
        'classes': ['video']
    }
    r = requests.post(agent_api + 'peripheral', json=mock_peripheral)
    assert r.status_code == 201, f'Unable to add new peripheral via {agent_api}'

    mock_peripheral_id = r.json()['resource-id']

    r = requests.get(agent_api + 'peripheral')
    assert r.status_code == 200, f'Unable to get peripherals via {agent_api}'
    assert mock_peripheral_identifier in r.json(), f'Wrong format while getting peripherals via {agent_api}'

    r = requests.get(agent_api + 'peripheral/' + mock_peripheral_identifier)
    assert r.status_code == 200, f'Unable to get specific peripheral via {agent_api}'
    assert isinstance(r.json(), dict), f'Wrong format while getting peripheral via {agent_api}'
    assert mock_peripheral_id == r.json()['id'], \
        f'Peripheral returned by {agent_api} does not match ID {mock_peripheral_id}'

    new_description = 'new test description'
    r = requests.put(agent_api + 'peripheral/' + mock_peripheral_identifier, json={'description': new_description})
    assert r.status_code == 200, f'Unable to edit peripheral via {agent_api}'
    assert r.json().get('description', '') == new_description, 'Edited peripheral is not up to date'

    r = requests.get(agent_api + 'peripheral?identifier_pattern=mock*')
    assert r.status_code == 200, f'Unable to get peripheral by pattern via {agent_api}'
    assert isinstance(r.json(), dict), f'Wrong format while getting peripheral via {agent_api}'
    assert mock_peripheral_id == r.json()[mock_peripheral_identifier]['id'], \
        f'Peripheral returned by {agent_api} does not match ID {mock_peripheral_id}'

    r = requests.get(agent_api + 'peripheral?identifier_pattern=bad-peripheral')
    assert r.status_code == 200, f'Unable to get peripheral by pattern via {agent_api}'
    assert not r.json(), f'{agent_api} returned some peripheral while it should have returned None'

    # 2nd peripheral
    mock_peripheral_identifier_two = 'mock:usb:peripheral:2'
    mock_peripheral.update({
        'identifier': mock_peripheral_identifier_two,
        'name': '(local NB) Mock USB Peripheral 2'
    })
    r = requests.post(agent_api + 'peripheral', json=mock_peripheral)
    assert r.status_code == 201, f'Unable to add new peripheral via {agent_api}'

    request.config.cache.set('peripheral_id', r.json()['resource-id'])

    r = requests.get(agent_api + 'peripheral?identifier_pattern=mock*')
    assert r.status_code == 200, f'Unable to get peripheral by pattern via {agent_api}'
    assert isinstance(r.json(), dict), f'Wrong format while getting peripheral via {agent_api}'
    assert len(r.json().keys()) == 2, f'{agent_api} should have returned 2 peripherals'

    r = requests.get(agent_api + 'peripheral?parameter=description&value=' + new_description)
    assert r.status_code == 200, f'Unable to get peripheral by param/value via {agent_api}'
    assert isinstance(r.json(), dict), f'Wrong format while getting peripheral via {agent_api}'
    assert len(r.json().keys()) == 1, 'Returned peripherals that do not match param/value'

    r = requests.delete(agent_api + 'peripheral/' + mock_peripheral_identifier)
    assert r.status_code == r.json()['status'] == 200, f'Cannot delete peripheral via {agent_api}'
    assert r.json()['resource-id'] == mock_peripheral_id, f'ID mismatch while deleting peripheral via {agent_api}'

    with pytest.raises(nuvla.api.api.NuvlaError):
        api.get(mock_peripheral_id)

    logging.info(f'Agent API ({agent_api}) for peripheral management is up and running')


def test_nuvlabox_engine_local_management_api(request, remote):
    isg_id = request.config.cache.get('nuvlabox_id_local_isg', '')

    infra_service_group = api.get(isg_id)
    local_nb_credential = None
    for infra in infra_service_group.data['infrastructure-services']:
        infra_service = api.get(infra['href'])
        if infra_service.data['subtype'] == "swarm":
            cred_subtype = "infrastructure-service-swarm"
            query = f'parent="{infra_service.id}" and subtype="{cred_subtype}"'
            local_nb_credential = api.get('credential',
                                          filter=query)
            assert local_nb_credential.data['count'] == 1, \
                f'Cannot find credential for NuvlaBox in {api.endpoint}, with query: {query}'
            break

    assert local_nb_credential is not None, f'Retrieved false NuvlaBox credential'
    cert.write(local_nb_credential.data['resources'][0]['cert'].encode())
    cert.flush()

    key.write(local_nb_credential.data['resources'][0]['key'].encode())
    key.flush()

    management_api = 'https://localhost:5001/api'
    r = requests.get(management_api, verify=False, cert=(cert.name, key.name))

    assert r.status_code == 200, f'NuvlaBox management API {management_api} is not working'
    assert 'nuvlabox-api-endpoints' in r.json(), f'NuvlaBox management API {management_api} did not return JSON'
    logging.info(f'Management API ({management_api}) is up, running and secured')

    # won't do the test on add/revoke-ssh-key because it is a sensitive action
    # let's not compromise the test environment
    # let's not compromise the test environment

    peripheral_id = request.config.cache.get('peripheral_id', '')
    assert peripheral_id != '', 'PyTest cache is not working'

    r = requests.post(management_api + '/data-source-mjpg/enable',
                      verify=False,
                      cert=(cert.name, key.name),
                      json={'id': peripheral_id, 'video-device': '/dev/null'})
    assert r.status_code == 200, f'NuvlaBox management API {management_api} failed to start mjgp streamer: {r.text}'

    r = requests.post(management_api + '/data-source-mjpg/disable',
                      verify=False,
                      cert=(cert.name, key.name),
                      json={'id': peripheral_id})
    atexit.register(cleaner.delete_zombie_mjpg_streamer, peripheral_id)
    assert r.status_code == 200, f'NuvlaBox management API {management_api} failed to stop mjgp streamer: {r.text}'
    atexit.unregister(cleaner.delete_zombie_mjpg_streamer)

    logging.info(f'Management API ({management_api}) succeeded at handling Data Gateway video streaming requests')

    if remote:
        nuvlabox_id = request.config.cache.get('nuvlabox_id_remote', '')

        # check-api
        check_api = api.get(nuvlabox_id + "/check-api")
        assert check_api.data.get('status') == 202, f'Failed to launch NuvlaBox API check via {api.endpoint}'

        job_id = check_api.data['location']

        with timeout(60):
            while True:
                job = api.get(job_id)
                assert job.data.get('state', '').lower() != "failed", f'Failed to check NuvlaBox API via {api.endpoint}'
                if job.data.get('state', '') == 'SUCCESS':
                    break

                time.sleep(5)

        logging.info(f'Management API ({management_api}) is up, running and secured - remote check from {api.endpoint}')


def test_nuvlabox_engine_local_compute_api(request):
    compute_api = 'https://localhost:5000/'

    r = requests.get(compute_api + 'containers/json', verify=False, cert=(cert.name, key.name))
    assert r.status_code == 200, f'NuvlaBox compute API {compute_api} is not working'
    assert len(r.json()) > 0, \
        f'NuvlaBox compute API {compute_api} should have reported (at least) the NuvlaBox containers'

    logging.info(f'Compute API ({compute_api}) is up, running and secured')


def test_nuvlabox_engine_local_datagateway():
    nuvlabox_network = 'nuvlabox-shared-network'

    check_dg = docker_client.containers.run('alpine',
                                            command='sh -c "ping -c 1 data-gateway 2>&1 >/dev/null && echo OK"',
                                            network=nuvlabox_network,
                                            remove=True)

    assert 'OK' in check_dg.decode(), f'Cannot reach Data Gateway containers: {check_dg}'

    logging.info(f'NuvlaBox shared network ({nuvlabox_network}) is functional')

    cmd = 'sh -c "apk add mosquitto-clients >/dev/null && mosquitto_sub -h datagateway -t nuvlabox-status -C 1"'
    check_mqtt = docker_client.containers.run('alpine',
                                              command=cmd,
                                              network=nuvlabox_network,
                                              remove=True)

    nb_status = json.loads(check_mqtt.decode())
    assert nb_status['status'] == 'OPERATIONAL', f'MQTT check of the NuvlaBox status failed: {nb_status}'

    logging.info('NuvlaBox MQTT messaging is working')


def test_nuvlabox_engine_local_system_manager():
    system_manager_dashboard = 'http://127.0.0.1:3636/dashboard'

    r = requests.get(system_manager_dashboard)
    assert r.status_code == 200, f'Internal NuvlaBox dashboard {system_manager_dashboard} is down'
    assert 'NuvlaBox Local Dashboard' in r.text, \
        f'Internal NuvlaBox dashboard {system_manager_dashboard} has wrong content'

    r = requests.get(system_manager_dashboard + '/logs')
    assert r.status_code == 200, f'Internal NuvlaBox dashboard {system_manager_dashboard}/logs is down'
    assert 'NuvlaBox Local Dashboard - Logs' in r.text, \
        f'Internal NuvlaBox dashboard {system_manager_dashboard}/logs has wrong content'

    r = requests.get(system_manager_dashboard + '/peripherals')
    assert r.status_code == 200, f'Internal NuvlaBox dashboard {system_manager_dashboard}/peripherals is down'
    assert 'NuvlaBox Local Dashboard - Peripherals' in r.text, \
        f'Internal NuvlaBox dashboard {system_manager_dashboard}/peripherals has wrong content'

    logging.info(f'NuvlaBox internal dashboard ({system_manager_dashboard}) is up and running')


def test_nuvlabox_engine_remote_system_manager(remote):
    if not remote:
        logging.info('Remote testing disabled')
    else:
        system_manager_dashboard = 'http://swarm.nuvla.io:3636/dashboard'

        with pytest.raises(requests.exceptions.ConnectionError):
            requests.get(system_manager_dashboard)

        logging.info(f'NuvlaBox internal dashboard {system_manager_dashboard} inaccessible from outside, as expected')


def test_cis_benchmark(request, cis, nolinux):
    if not cis:
        logging.info('CIS Benchmark not selected')
    else:
        if nolinux:
            logging.warning('CIS Benchmark can only run on Linux machines. Skipping it')
        else:
            containers = request.config.cache.get('containers', [])
            images = request.config.cache.get('images', [])

            log_file = '/tmp/cis_log'
            cmd = f'-l {log_file} -c container_images,container_runtime -i {",".join(containers)} -t {",".join(images)}'
            docker_client.containers.run('docker/docker-bench-security',
                                         network_mode='host',
                                         pid_mode='host',
                                         userns_mode='host',
                                         remove=True,
                                         cap_add='audit_control',
                                         environment=[f'DOCKER_CONTENT_TRUST={os.getenv("DOCKER_CONTENT_TRUST")}'],
                                         volumes={
                                             '/etc': {'bind': '/etc', 'mode': 'ro'},
                                             '/usr/bin/docker-containerd': {'bind': '/usr/bin/docker-containerd',
                                                                            'mode': 'ro'},
                                             '/usr/bin/runc': {'bind': '/usr/bin/runc', 'mode': 'ro'},
                                             '/usr/lib/systemd': {'bind': '/usr/lib/systemd', 'mode': 'ro'},
                                             '/var/lib': {'bind': '/var/lib', 'mode': 'ro'},
                                             '/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'ro'},
                                             '/tmp': {'bind': '/tmp', 'mode': 'rw'}
                                         },
                                         labels=["docker_bench_security"],
                                         command=cmd)

            with open(log_file) as r:
                out = r.readlines()

            score = 0
            reference_score = -7
            for line in out:
                if 'Score: ' in line:
                    score = int(line.strip().split(' ')[-1])

            assert score >= reference_score, \
                f'CIS benchmark failed with a low score of {score}. Containers: {containers}. Images: {images}'
            logging.info(f'CIS benchmark finished with a final score of {score}')


def test_snyk_score(request):
    logging.info('Running Snyk scan with the API token provided from the environment')
    snyktoken = os.getenv('SNYK_SIXSQCI_API_TOKEN')
    assert snyktoken is not None, f'Invalid Snyk token provided'

    images = request.config.cache.get('images', [])

    total_high_vulnerabilities = 0

    # count of current high vulnerabilities
    reference_vulnerabilities = 66
    log_file = 'log.json'
    for img in images:
        os.system(f'SNYK_TOKEN={snyktoken} snyk test --docker {img} --json --severity-threshold=high > {log_file}')
        with open(log_file) as l:
            out = json.load(l)

        total_high_vulnerabilities += out['uniqueCount']

    assert total_high_vulnerabilities <= reference_vulnerabilities, \
        f'Snyk scan detected more high vulnerabilities ({total_high_vulnerabilities}) ' \
            f'than expected ({reference_vulnerabilities})'
    logging.info(f'Snyk: number of high vulnerabilities found ({total_high_vulnerabilities}) is not higher'
                 f'than previous tests ({reference_vulnerabilities})')
