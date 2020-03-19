import datetime
import json
import logging
import os.path
import ssl
import sys
import shutil
import tempfile
import warnings

import mock
import pytest
import trustme

from dummyserver.testcase import HTTPSDummyServerTestCase
from dummyserver.server import (
    encrypt_key_pem,
    DEFAULT_CA,
    DEFAULT_CA_KEY,
    DEFAULT_CERTS,
)

from test import (
    onlyPy279OrNewer,
    notSecureTransport,
    notOpenSSL098,
    requires_network,
    requires_ssl_context_keyfile_password,
    fails_on_travis_gce,
    requiresTLSv1,
    requiresTLSv1_1,
    requiresTLSv1_2,
    requiresTLSv1_3,
    TARPIT_HOST,
    SHORT_TIMEOUT,
    LONG_TIMEOUT,
)
from urllib3 import HTTPSConnectionPool
from urllib3.connection import VerifiedHTTPSConnection, RECENT_DATE
from urllib3.exceptions import (
    SSLError,
    ConnectTimeoutError,
    InsecureRequestWarning,
    SystemTimeWarning,
    InsecurePlatformWarning,
    MaxRetryError,
    ProtocolError,
)
from urllib3.packages import six
from urllib3.util.timeout import Timeout
import urllib3.util as util

# Retry failed tests
pytestmark = pytest.mark.flaky

ResourceWarning = getattr(
    six.moves.builtins, "ResourceWarning", type("ResourceWarning", (), {})
)


log = logging.getLogger("urllib3.connectionpool")
log.setLevel(logging.NOTSET)
log.addHandler(logging.StreamHandler(sys.stdout))


TLSv1_CERTS = DEFAULT_CERTS.copy()
TLSv1_CERTS["ssl_version"] = getattr(ssl, "PROTOCOL_TLSv1", None)

TLSv1_1_CERTS = DEFAULT_CERTS.copy()
TLSv1_1_CERTS["ssl_version"] = getattr(ssl, "PROTOCOL_TLSv1_1", None)

TLSv1_2_CERTS = DEFAULT_CERTS.copy()
TLSv1_2_CERTS["ssl_version"] = getattr(ssl, "PROTOCOL_TLSv1_2", None)

TLSv1_3_CERTS = DEFAULT_CERTS.copy()
TLSv1_3_CERTS["ssl_version"] = getattr(ssl, "PROTOCOL_TLS", None)


CLIENT_INTERMEDIATE_PEM = "client_intermediate.pem"
CLIENT_NO_INTERMEDIATE_PEM = "client_no_intermediate.pem"
CLIENT_INTERMEDIATE_KEY = "client_intermediate.key"
PASSWORD_CLIENT_KEYFILE = "client_password.key"
CLIENT_CERT = CLIENT_INTERMEDIATE_PEM


class TestHTTPS(HTTPSDummyServerTestCase):
    tls_protocol_name = None

    @classmethod
    def setup_class(cls):
        super(TestHTTPS, cls).setup_class()

        cls.certs_dir = tempfile.mkdtemp()
        # Start from existing root CA as we don't want to change the server certificate yet
        with open(DEFAULT_CA, "rb") as crt, open(DEFAULT_CA_KEY, "rb") as key:
            root_ca = trustme.CA.from_pem(crt.read(), key.read())

        # Generate another CA to test verification failure
        bad_ca = trustme.CA()
        cls.bad_ca_path = os.path.join(cls.certs_dir, "ca_bad.pem")
        bad_ca.cert_pem.write_to_path(cls.bad_ca_path)

        # client cert chain
        intermediate_ca = root_ca.create_child_ca()
        cert = intermediate_ca.issue_cert(u"example.com")
        encrypted_key = encrypt_key_pem(cert.private_key_pem, b"letmein")

        cert.private_key_pem.write_to_path(
            os.path.join(cls.certs_dir, CLIENT_INTERMEDIATE_KEY)
        )
        encrypted_key.write_to_path(
            os.path.join(cls.certs_dir, PASSWORD_CLIENT_KEYFILE)
        )
        # Write the client cert and the intermediate CA
        client_cert = os.path.join(cls.certs_dir, CLIENT_INTERMEDIATE_PEM)
        cert.cert_chain_pems[0].write_to_path(client_cert)
        cert.cert_chain_pems[1].write_to_path(client_cert, append=True)
        # Write only the client cert
        cert.cert_chain_pems[0].write_to_path(
            os.path.join(cls.certs_dir, CLIENT_NO_INTERMEDIATE_PEM)
        )

    @classmethod
    def teardown_class(cls):
        super(TestHTTPS, cls).teardown_class()

        shutil.rmtree(cls.certs_dir)

    def test_simple(self):
        with HTTPSConnectionPool(
            self.host, self.port, ca_certs=DEFAULT_CA
        ) as https_pool:
            r = https_pool.request("GET", "/")
            assert r.status == 200, r.data

    @fails_on_travis_gce
    def test_dotted_fqdn(self):
        with HTTPSConnectionPool(
            self.host + ".", self.port, ca_certs=DEFAULT_CA
        ) as pool:
            r = pool.request("GET", "/")
            assert r.status == 200, r.data

    def test_client_intermediate(self):
        """Check that certificate chains work well with client certs

        We generate an intermediate CA from the root CA, and issue a client certificate
        from that intermediate CA. Since the server only knows about the root CA, we
        need to send it the certificate *and* the intermediate CA, so that it can check
        the whole chain.
        """
        with HTTPSConnectionPool(
            self.host,
            self.port,
            key_file=os.path.join(self.certs_dir, CLIENT_INTERMEDIATE_KEY),
            cert_file=os.path.join(self.certs_dir, CLIENT_INTERMEDIATE_PEM),
            ca_certs=DEFAULT_CA,
        ) as https_pool:
            r = https_pool.request("GET", "/certificate")
            subject = json.loads(r.data.decode("utf-8"))
            assert subject["organizationalUnitName"].startswith("Testing cert")

    def test_client_no_intermediate(self):
        """Check that missing links in certificate chains indeed break

        The only difference with test_client_intermediate is that we don't send the
        intermediate CA to the server, only the client cert.
        """
        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_file=os.path.join(self.certs_dir, CLIENT_NO_INTERMEDIATE_PEM),
            key_file=os.path.join(self.certs_dir, CLIENT_INTERMEDIATE_KEY),
            ca_certs=DEFAULT_CA,
        ) as https_pool:
            with pytest.raises((SSLError, ProtocolError)):
                https_pool.request("GET", "/certificate", retries=False)

    @requires_ssl_context_keyfile_password
    def test_client_key_password(self):
        with HTTPSConnectionPool(
            self.host,
            self.port,
            ca_certs=DEFAULT_CA,
            key_file=os.path.join(self.certs_dir, PASSWORD_CLIENT_KEYFILE),
            cert_file=os.path.join(self.certs_dir, CLIENT_CERT),
            key_password="letmein",
        ) as https_pool:
            r = https_pool.request("GET", "/certificate")
            subject = json.loads(r.data.decode("utf-8"))
            assert subject["organizationalUnitName"].startswith("Testing cert")

    @requires_ssl_context_keyfile_password
    def test_client_encrypted_key_requires_password(self):
        with HTTPSConnectionPool(
            self.host,
            self.port,
            key_file=os.path.join(self.certs_dir, PASSWORD_CLIENT_KEYFILE),
            cert_file=os.path.join(self.certs_dir, CLIENT_CERT),
            key_password=None,
        ) as https_pool:
            with pytest.raises(MaxRetryError) as e:
                https_pool.request("GET", "/certificate")

            assert "password is required" in str(e.value)
            assert isinstance(e.value.reason, SSLError)

    def test_verified(self):
        with HTTPSConnectionPool(
            self.host, self.port, cert_reqs="CERT_REQUIRED", ca_certs=DEFAULT_CA
        ) as https_pool:
            conn = https_pool._new_conn()
            assert conn.__class__ == VerifiedHTTPSConnection

            with mock.patch("warnings.warn") as warn:
                r = https_pool.request("GET", "/")
                assert r.status == 200

                # Modern versions of Python, or systems using PyOpenSSL, don't
                # emit warnings.
                if (
                    sys.version_info >= (2, 7, 9)
                    or util.IS_PYOPENSSL
                    or util.IS_SECURETRANSPORT
                ):
                    assert not warn.called, warn.call_args_list
                else:
                    assert warn.called
                    if util.HAS_SNI:
                        call = warn.call_args_list[0]
                    else:
                        call = warn.call_args_list[1]
                    error = call[0][1]
                    assert error == InsecurePlatformWarning

    def test_verified_with_context(self):
        ctx = util.ssl_.create_urllib3_context(cert_reqs=ssl.CERT_REQUIRED)
        ctx.load_verify_locations(cafile=DEFAULT_CA)
        with HTTPSConnectionPool(self.host, self.port, ssl_context=ctx) as https_pool:
            conn = https_pool._new_conn()
            assert conn.__class__ == VerifiedHTTPSConnection

            with mock.patch("warnings.warn") as warn:
                r = https_pool.request("GET", "/")
                assert r.status == 200

                # Modern versions of Python, or systems using PyOpenSSL, don't
                # emit warnings.
                if (
                    sys.version_info >= (2, 7, 9)
                    or util.IS_PYOPENSSL
                    or util.IS_SECURETRANSPORT
                ):
                    assert not warn.called, warn.call_args_list
                else:
                    assert warn.called
                    if util.HAS_SNI:
                        call = warn.call_args_list[0]
                    else:
                        call = warn.call_args_list[1]
                    error = call[0][1]
                    assert error == InsecurePlatformWarning

    def test_context_combines_with_ca_certs(self):
        ctx = util.ssl_.create_urllib3_context(cert_reqs=ssl.CERT_REQUIRED)
        with HTTPSConnectionPool(
            self.host, self.port, ca_certs=DEFAULT_CA, ssl_context=ctx
        ) as https_pool:
            conn = https_pool._new_conn()
            assert conn.__class__ == VerifiedHTTPSConnection

            with mock.patch("warnings.warn") as warn:
                r = https_pool.request("GET", "/")
                assert r.status == 200

                # Modern versions of Python, or systems using PyOpenSSL, don't
                # emit warnings.
                if (
                    sys.version_info >= (2, 7, 9)
                    or util.IS_PYOPENSSL
                    or util.IS_SECURETRANSPORT
                ):
                    assert not warn.called, warn.call_args_list
                else:
                    assert warn.called
                    if util.HAS_SNI:
                        call = warn.call_args_list[0]
                    else:
                        call = warn.call_args_list[1]
                    error = call[0][1]
                    assert error == InsecurePlatformWarning

    @onlyPy279OrNewer
    @notSecureTransport  # SecureTransport does not support cert directories
    @notOpenSSL098  # OpenSSL 0.9.8 does not support cert directories
    def test_ca_dir_verified(self, tmpdir):
        # OpenSSL looks up certificates by the hash for their name, see c_rehash
        # TODO infer the bytes using `cryptography.x509.Name.public_bytes`.
        # https://github.com/pyca/cryptography/pull/3236
        shutil.copyfile(DEFAULT_CA, str(tmpdir / "b6b9ccf9.0"))

        with HTTPSConnectionPool(
            self.host, self.port, cert_reqs="CERT_REQUIRED", ca_cert_dir=str(tmpdir)
        ) as https_pool:
            conn = https_pool._new_conn()
            assert conn.__class__ == VerifiedHTTPSConnection

            with mock.patch("warnings.warn") as warn:
                r = https_pool.request("GET", "/")
                assert r.status == 200
                assert not warn.called, warn.call_args_list

    def test_invalid_common_name(self):
        with HTTPSConnectionPool(
            "127.0.0.1", self.port, cert_reqs="CERT_REQUIRED", ca_certs=DEFAULT_CA
        ) as https_pool:
            with pytest.raises(MaxRetryError) as e:
                https_pool.request("GET", "/")
            assert isinstance(e.value.reason, SSLError)
            assert "doesn't match" in str(
                e.value.reason
            ) or "certificate verify failed" in str(e.value.reason)

    def test_verified_with_bad_ca_certs(self):
        with HTTPSConnectionPool(
            self.host, self.port, cert_reqs="CERT_REQUIRED", ca_certs=self.bad_ca_path
        ) as https_pool:
            with pytest.raises(MaxRetryError) as e:
                https_pool.request("GET", "/")
            assert isinstance(e.value.reason, SSLError)
            assert "certificate verify failed" in str(e.value.reason), (
                "Expected 'certificate verify failed', instead got: %r" % e.value.reason
            )

    def test_verified_without_ca_certs(self):
        # default is cert_reqs=None which is ssl.CERT_NONE
        with HTTPSConnectionPool(
            self.host, self.port, cert_reqs="CERT_REQUIRED"
        ) as https_pool:
            with pytest.raises(MaxRetryError) as e:
                https_pool.request("GET", "/")
            assert isinstance(e.value.reason, SSLError)
            # there is a different error message depending on whether or
            # not pyopenssl is injected
            assert (
                "No root certificates specified" in str(e.value.reason)
                # PyPy sometimes uses all-caps here
                or "certificate verify failed" in str(e.value.reason).lower()
                or "invalid certificate chain" in str(e.value.reason)
            ), (
                "Expected 'No root certificates specified',  "
                "'certificate verify failed', or "
                "'invalid certificate chain', "
                "instead got: %r" % e.value.reason
            )

    def test_no_ssl(self):
        with HTTPSConnectionPool(self.host, self.port) as pool:
            pool.ConnectionCls = None
            with pytest.raises(SSLError):
                pool._new_conn()
            with pytest.raises(MaxRetryError) as cm:
                pool.request("GET", "/", retries=0)
            assert isinstance(cm.value.reason, SSLError)

    def test_unverified_ssl(self):
        """ Test that bare HTTPSConnection can connect, make requests """
        with HTTPSConnectionPool(self.host, self.port, cert_reqs=ssl.CERT_NONE) as pool:
            with mock.patch("warnings.warn") as warn:
                r = pool.request("GET", "/")
                assert r.status == 200
                assert warn.called

                # Modern versions of Python, or systems using PyOpenSSL, only emit
                # the unverified warning. Older systems may also emit other
                # warnings, which we want to ignore here.
                calls = warn.call_args_list
                assert InsecureRequestWarning in [x[0][1] for x in calls]

    def test_ssl_unverified_with_ca_certs(self):
        with HTTPSConnectionPool(
            self.host, self.port, cert_reqs="CERT_NONE", ca_certs=self.bad_ca_path
        ) as pool:
            with mock.patch("warnings.warn") as warn:
                r = pool.request("GET", "/")
                assert r.status == 200
                assert warn.called

                # Modern versions of Python, or systems using PyOpenSSL, only emit
                # the unverified warning. Older systems may also emit other
                # warnings, which we want to ignore here.
                calls = warn.call_args_list
                if (
                    sys.version_info >= (2, 7, 9)
                    or util.IS_PYOPENSSL
                    or util.IS_SECURETRANSPORT
                ):
                    category = calls[0][0][1]
                elif util.HAS_SNI:
                    category = calls[1][0][1]
                else:
                    category = calls[2][0][1]
                assert category == InsecureRequestWarning

    def test_assert_hostname_false(self):
        with HTTPSConnectionPool(
            "localhost", self.port, cert_reqs="CERT_REQUIRED", ca_certs=DEFAULT_CA
        ) as https_pool:
            https_pool.assert_hostname = False
            https_pool.request("GET", "/")

    def test_assert_specific_hostname(self):
        with HTTPSConnectionPool(
            "localhost", self.port, cert_reqs="CERT_REQUIRED", ca_certs=DEFAULT_CA
        ) as https_pool:
            https_pool.assert_hostname = "localhost"
            https_pool.request("GET", "/")

    def test_server_hostname(self):
        with HTTPSConnectionPool(
            "127.0.0.1",
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            server_hostname="localhost",
        ) as https_pool:
            conn = https_pool._new_conn()
            conn.request("GET", "/")

            # Assert the wrapping socket is using the passed-through SNI name.
            # pyopenssl doesn't let you pull the server_hostname back off the
            # socket, so only add this assertion if the attribute is there (i.e.
            # the python ssl module).
            if hasattr(conn.sock, "server_hostname"):
                assert conn.sock.server_hostname == "localhost"

    def test_assert_fingerprint_md5(self):
        with HTTPSConnectionPool(
            "localhost", self.port, cert_reqs="CERT_REQUIRED", ca_certs=DEFAULT_CA
        ) as https_pool:
            https_pool.assert_fingerprint = (
                "F2:06:5A:42:10:3F:45:1C:17:FE:E6:07:1E:8A:86:E5"
            )

            https_pool.request("GET", "/")

    def test_assert_fingerprint_sha1(self):
        with HTTPSConnectionPool(
            "localhost", self.port, cert_reqs="CERT_REQUIRED", ca_certs=DEFAULT_CA
        ) as https_pool:
            https_pool.assert_fingerprint = (
                "92:81:FE:85:F7:0C:26:60:EC:D6:B3:BF:93:CF:F9:71:CC:07:7D:0A"
            )
            https_pool.request("GET", "/")

    def test_assert_fingerprint_sha256(self):
        with HTTPSConnectionPool(
            "localhost", self.port, cert_reqs="CERT_REQUIRED", ca_certs=DEFAULT_CA
        ) as https_pool:
            https_pool.assert_fingerprint = (
                "C5:4D:0B:83:84:89:2E:AE:B4:58:BB:12:"
                "F7:A6:C4:76:05:03:88:D8:57:65:51:F3:"
                "1E:60:B0:8B:70:18:64:E6"
            )
            https_pool.request("GET", "/")

    def test_assert_invalid_fingerprint(self):
        with HTTPSConnectionPool(
            "127.0.0.1", self.port, cert_reqs="CERT_REQUIRED", ca_certs=DEFAULT_CA
        ) as https_pool:
            https_pool.assert_fingerprint = (
                "AA:AA:AA:AA:AA:AAAA:AA:AAAA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA"
            )

            def _test_request(pool):
                with pytest.raises(MaxRetryError) as cm:
                    pool.request("GET", "/", retries=0)
                assert isinstance(cm.value.reason, SSLError)

            _test_request(https_pool)
            https_pool._get_conn()

            # Uneven length
            https_pool.assert_fingerprint = "AA:A"
            _test_request(https_pool)
            https_pool._get_conn()

            # Invalid length
            https_pool.assert_fingerprint = "AA"
            _test_request(https_pool)

    def test_verify_none_and_bad_fingerprint(self):
        with HTTPSConnectionPool(
            "127.0.0.1", self.port, cert_reqs="CERT_NONE", ca_certs=self.bad_ca_path
        ) as https_pool:
            https_pool.assert_fingerprint = (
                "AA:AA:AA:AA:AA:AAAA:AA:AAAA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA"
            )
            with pytest.raises(MaxRetryError) as cm:
                https_pool.request("GET", "/", retries=0)
            assert isinstance(cm.value.reason, SSLError)

    def test_verify_none_and_good_fingerprint(self):
        with HTTPSConnectionPool(
            "127.0.0.1", self.port, cert_reqs="CERT_NONE", ca_certs=self.bad_ca_path
        ) as https_pool:
            https_pool.assert_fingerprint = (
                "92:81:FE:85:F7:0C:26:60:EC:D6:B3:BF:93:CF:F9:71:CC:07:7D:0A"
            )
            https_pool.request("GET", "/")

    @notSecureTransport
    def test_good_fingerprint_and_hostname_mismatch(self):
        # This test doesn't run with SecureTransport because we don't turn off
        # hostname validation without turning off all validation, which this
        # test doesn't do (deliberately). We should revisit this if we make
        # new decisions.
        with HTTPSConnectionPool(
            "127.0.0.1", self.port, cert_reqs="CERT_REQUIRED", ca_certs=DEFAULT_CA
        ) as https_pool:
            https_pool.assert_fingerprint = (
                "92:81:FE:85:F7:0C:26:60:EC:D6:B3:BF:93:CF:F9:71:CC:07:7D:0A"
            )
            https_pool.request("GET", "/")

    @requires_network
    def test_https_timeout(self):

        timeout = Timeout(total=None, connect=SHORT_TIMEOUT)
        with HTTPSConnectionPool(
            TARPIT_HOST,
            self.port,
            timeout=timeout,
            retries=False,
            cert_reqs="CERT_REQUIRED",
        ) as https_pool:
            with pytest.raises(ConnectTimeoutError):
                https_pool.request("GET", "/")

        timeout = Timeout(read=0.01)
        with HTTPSConnectionPool(
            self.host,
            self.port,
            timeout=timeout,
            retries=False,
            cert_reqs="CERT_REQUIRED",
        ) as https_pool:
            https_pool.ca_certs = DEFAULT_CA
            https_pool.assert_fingerprint = (
                "92:81:FE:85:F7:0C:26:60:EC:D6:B3:BF:93:CF:F9:71:CC:07:7D:0A"
            )

        timeout = Timeout(total=None)
        with HTTPSConnectionPool(
            self.host, self.port, timeout=timeout, cert_reqs="CERT_NONE"
        ) as https_pool:
            https_pool.request("GET", "/")

    def test_tunnel(self):
        """ test the _tunnel behavior """
        timeout = Timeout(total=None)
        with HTTPSConnectionPool(
            self.host, self.port, timeout=timeout, cert_reqs="CERT_NONE"
        ) as https_pool:
            conn = https_pool._new_conn()
            try:
                conn.set_tunnel(self.host, self.port)
                conn._tunnel = mock.Mock()
                https_pool._make_request(conn, "GET", "/")
                conn._tunnel.assert_called_once_with()
            finally:
                conn.close()

    @requires_network
    def test_enhanced_timeout(self):
        with HTTPSConnectionPool(
            TARPIT_HOST,
            self.port,
            timeout=Timeout(connect=SHORT_TIMEOUT),
            retries=False,
            cert_reqs="CERT_REQUIRED",
        ) as https_pool:
            conn = https_pool._new_conn()
            try:
                with pytest.raises(ConnectTimeoutError):
                    https_pool.request("GET", "/")
                with pytest.raises(ConnectTimeoutError):
                    https_pool._make_request(conn, "GET", "/")
            finally:
                conn.close()

        with HTTPSConnectionPool(
            TARPIT_HOST,
            self.port,
            timeout=Timeout(connect=LONG_TIMEOUT),
            retries=False,
            cert_reqs="CERT_REQUIRED",
        ) as https_pool:
            with pytest.raises(ConnectTimeoutError):
                https_pool.request("GET", "/", timeout=Timeout(connect=SHORT_TIMEOUT))

        with HTTPSConnectionPool(
            TARPIT_HOST,
            self.port,
            timeout=Timeout(total=None),
            retries=False,
            cert_reqs="CERT_REQUIRED",
        ) as https_pool:
            conn = https_pool._new_conn()
            try:
                with pytest.raises(ConnectTimeoutError):
                    https_pool.request(
                        "GET", "/", timeout=Timeout(total=None, connect=SHORT_TIMEOUT)
                    )
            finally:
                conn.close()

    def test_enhanced_ssl_connection(self):
        fingerprint = "92:81:FE:85:F7:0C:26:60:EC:D6:B3:BF:93:CF:F9:71:CC:07:7D:0A"

        with HTTPSConnectionPool(
            self.host,
            self.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=DEFAULT_CA,
            assert_fingerprint=fingerprint,
        ) as https_pool:
            r = https_pool.request("GET", "/")
            assert r.status == 200

    @onlyPy279OrNewer
    def test_ssl_correct_system_time(self):
        with HTTPSConnectionPool(
            self.host, self.port, ca_certs=DEFAULT_CA
        ) as https_pool:
            https_pool.cert_reqs = "CERT_REQUIRED"
            https_pool.ca_certs = DEFAULT_CA

            w = self._request_without_resource_warnings("GET", "/")
            assert [] == w

    @onlyPy279OrNewer
    def test_ssl_wrong_system_time(self):
        with HTTPSConnectionPool(
            self.host, self.port, ca_certs=DEFAULT_CA
        ) as https_pool:
            https_pool.cert_reqs = "CERT_REQUIRED"
            https_pool.ca_certs = DEFAULT_CA
            with mock.patch("urllib3.connection.datetime") as mock_date:
                mock_date.date.today.return_value = datetime.date(1970, 1, 1)

                w = self._request_without_resource_warnings("GET", "/")

                assert len(w) == 1
                warning = w[0]

                assert SystemTimeWarning == warning.category
                assert str(RECENT_DATE) in warning.message.args[0]

    def _request_without_resource_warnings(self, method, url):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            with HTTPSConnectionPool(
                self.host, self.port, ca_certs=DEFAULT_CA
            ) as https_pool:
                https_pool.request(method, url)

        return [x for x in w if not isinstance(x.message, ResourceWarning)]

    def test_set_ssl_version_to_tls_version(self):
        if self.tls_protocol_name is None:
            pytest.skip("Skipping base test class")

        with HTTPSConnectionPool(
            self.host, self.port, ca_certs=DEFAULT_CA
        ) as https_pool:
            https_pool.ssl_version = self.certs["ssl_version"]
            r = https_pool.request("GET", "/")
            assert r.status == 200, r.data

    def test_set_cert_default_cert_required(self):
        conn = VerifiedHTTPSConnection(self.host, self.port)
        conn.set_cert()
        assert conn.cert_reqs == ssl.CERT_REQUIRED

    def test_tls_protocol_name_of_socket(self):
        if self.tls_protocol_name is None:
            pytest.skip("Skipping base test class")

        with HTTPSConnectionPool(
            self.host, self.port, ca_certs=DEFAULT_CA
        ) as https_pool:
            conn = https_pool._get_conn()
            try:
                conn.connect()
                if not hasattr(conn.sock, "version"):
                    pytest.skip("SSLSocket.version() not available")
                assert conn.sock.version() == self.tls_protocol_name
            finally:
                conn.close()


@requiresTLSv1()
class TestHTTPS_TLSv1(TestHTTPS):
    tls_protocol_name = "TLSv1"
    certs = TLSv1_CERTS


@requiresTLSv1_1()
class TestHTTPS_TLSv1_1(TestHTTPS):
    tls_protocol_name = "TLSv1.1"
    certs = TLSv1_1_CERTS


@requiresTLSv1_2()
class TestHTTPS_TLSv1_2(TestHTTPS):
    tls_protocol_name = "TLSv1.2"
    certs = TLSv1_2_CERTS


@requiresTLSv1_3()
class TestHTTPS_TLSv1_3(TestHTTPS):
    tls_protocol_name = "TLSv1.3"
    certs = TLSv1_3_CERTS


class TestHTTPS_NoSAN:
    def test_warning_for_certs_without_a_san(self, no_san_server):
        """Ensure that a warning is raised when the cert from the server has
        no Subject Alternative Name."""
        with mock.patch("warnings.warn") as warn:
            with HTTPSConnectionPool(
                no_san_server.host,
                no_san_server.port,
                cert_reqs="CERT_REQUIRED",
                ca_certs=no_san_server.ca_certs,
            ) as https_pool:
                r = https_pool.request("GET", "/")
                assert r.status == 200
                assert warn.called


class TestHTTPS_IPSAN:
    def test_can_validate_ip_san(self, ip_san_server):
        """Ensure that urllib3 can validate SANs with IP addresses in them."""
        try:
            import ipaddress  # noqa: F401
        except ImportError:
            pytest.skip("Only runs on systems with an ipaddress module")

        with HTTPSConnectionPool(
            ip_san_server.host,
            ip_san_server.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=ip_san_server.ca_certs,
        ) as https_pool:
            r = https_pool.request("GET", "/")
            assert r.status == 200


class TestHTTPS_IPv6Addr:
    def test_strip_square_brackets_before_validating(self, ipv6_addr_server):
        """Test that the fix for #760 works."""
        with HTTPSConnectionPool(
            "[::1]",
            ipv6_addr_server.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=ipv6_addr_server.ca_certs,
        ) as https_pool:
            r = https_pool.request("GET", "/")
            assert r.status == 200


class TestHTTPS_IPV6SAN:
    def test_can_validate_ipv6_san(self, ipv6_san_server):
        """Ensure that urllib3 can validate SANs with IPv6 addresses in them."""
        try:
            import ipaddress  # noqa: F401
        except ImportError:
            pytest.skip("Only runs on systems with an ipaddress module")

        with HTTPSConnectionPool(
            "[::1]",
            ipv6_san_server.port,
            cert_reqs="CERT_REQUIRED",
            ca_certs=ipv6_san_server.ca_certs,
        ) as https_pool:
            r = https_pool.request("GET", "/")
            assert r.status == 200
