def pytest_addoption(parser):
    parser.addoption("--apikey", action="store", default=None)
    parser.addoption("--apisecret", action="store", default=None)
    parser.addoption("--vpnserver", action="store", default=None)


def pytest_generate_tests(metafunc):
    # This is called for every test. Only get/set command line arguments
    # if the argument is specified in the list of test "fixturenames".
    option_value = metafunc.config.option.apikey
    if 'apikey' in metafunc.fixturenames and option_value is not None:
        metafunc.parametrize("apikey", [option_value])

    option_value = metafunc.config.option.apisecret
    if 'apisecret' in metafunc.fixturenames and option_value is not None:
        metafunc.parametrize("apisecret", [option_value])

    if 'vpnserver' in metafunc.fixturenames:
        metafunc.parametrize("vpnserver", [metafunc.config.option.vpnserver])
