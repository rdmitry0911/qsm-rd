# SPDX-License-Identifier: AGPL-3.0-or-later
#
# q-sunshine's deliberately small PVE-side Console endpoint.
#
# PVE 9 does not currently expose a public API-route plug-in ABI.  The
# package-owned pveproxy/pvedaemon launchers import this module before they
# start PVE's stock service classes.  The endpoint itself deliberately does
# not know how to authenticate a user: PVE's normal API session and the exact
# same VM.Console ACL predicate as PVE's native console methods are the only
# authorization input.
package PVE::API2::QSunshine;

use strict;
use warnings;

use IO::Select;
use IO::Socket::UNIX qw(SOCK_STREAM);
use JSON qw(decode_json encode_json);
use Fcntl qw(S_ISSOCK);

use PVE::API2::Qemu;
use PVE::JSONSchema qw(get_standard_option);
use PVE::QemuConfig;
use PVE::QemuServer;
use PVE::RPCEnvironment;

my $PVE_LAUNCH_SOCKET = '/run/q-sunshine-terminal/pve-launch.sock';
my $SOCKET_TIMEOUT_SECONDS = 3;
my $MAX_RESPONSE_BYTES = 96 * 1024;
my $MIN_VMID = 100;
my $MAX_VMID = 999_999_999;

sub _unavailable {
    # Do not reflect a filesystem path, PVE user name, socket response, or
    # transport claim into the PVE browser/API response.  The Console overlay
    # intentionally presents this as one generic launch failure.
    die "q-sunshine console is unavailable\n";
}

sub _private_socket_is_safe {
    my ($path) = @_;
    my @stat = lstat($path);
    return 0 if !@stat;

    # The terminal service creates this endpoint after PVE's own RuntimeDir
    # exists.  Refuse a symlink, non-socket, non-root owner, or any group/world
    # access rather than allowing a partially installed local service to
    # receive an ACL-authorized descriptor request.
    return 0 if !S_ISSOCK($stat[2]);
    return 0 if $stat[4] != 0;
    return 0 if $stat[2] & 0o077;
    return 1;
}

sub _write_all {
    my ($socket, $bytes) = @_;
    my $offset = 0;
    while ($offset < length($bytes)) {
        my $written = syswrite($socket, $bytes, length($bytes) - $offset, $offset);
        _unavailable() if !defined($written) || $written <= 0;
        $offset += $written;
    }
}

sub _read_one_line {
    my ($socket) = @_;
    my $selector = IO::Select->new($socket);
    my $response = '';
    my $deadline = time() + $SOCKET_TIMEOUT_SECONDS;

    while (1) {
        my $remaining = $deadline - time();
        _unavailable() if $remaining <= 0;
        my @ready = $selector->can_read($remaining);
        _unavailable() if !@ready;

        my $chunk = '';
        my $read = sysread($socket, $chunk, 4096);
        _unavailable() if !defined($read) || $read <= 0;
        $response .= $chunk;
        _unavailable() if length($response) > $MAX_RESPONSE_BYTES;

        my $newline = index($response, "\n");
        next if $newline < 0;
        _unavailable() if $newline != length($response) - 1;
        return substr($response, 0, $newline);
    }
}

sub _valid_descriptor {
    my ($descriptor) = @_;
    return 0 if ref($descriptor) ne 'HASH';
    return 0 if join("\0", sort keys($descriptor->%*)) ne
        join("\0", qw(claim endpoint expires_at_utc_ms kind version));
    return 0 if !defined($descriptor->{version}) || $descriptor->{version} != 1;
    return 0 if !defined($descriptor->{kind}) || $descriptor->{kind} ne 'q-sunshine-pve-launch';
    return 0 if ref($descriptor->{claim}) || $descriptor->{claim} !~ /\A[A-Za-z0-9._-]{16,4096}\z/;
    return 0 if ref($descriptor->{expires_at_utc_ms}) ||
        $descriptor->{expires_at_utc_ms} !~ /\A[0-9]{13,16}\z/;

    my $endpoint = $descriptor->{endpoint};
    return 0 if ref($endpoint) ne 'HASH';
    return 0 if join("\0", sort keys($endpoint->%*)) ne
        join("\0", qw(ca_pem host port server_name));
    return 0 if ref($endpoint->{host}) || !defined($endpoint->{host}) ||
        $endpoint->{host} !~ /\A[A-Za-z0-9.:-]{1,253}\z/;
    return 0 if ref($endpoint->{server_name}) || !defined($endpoint->{server_name}) ||
        $endpoint->{server_name} !~ /\A[A-Za-z0-9.:-]{1,253}\z/;
    return 0 if ref($endpoint->{port}) || !defined($endpoint->{port}) ||
        $endpoint->{port} !~ /\A[1-9][0-9]{0,4}\z/ || $endpoint->{port} > 65535;
    return 0 if ref($endpoint->{ca_pem}) || !defined($endpoint->{ca_pem}) ||
        length($endpoint->{ca_pem}) < 32 || length($endpoint->{ca_pem}) > 65536 ||
        index($endpoint->{ca_pem}, '-----BEGIN CERTIFICATE-----') < 0;
    return 1;
}

sub _launch_descriptor_from_terminal {
    my ($node, $vmid, $subject) = @_;
    _unavailable() if !_private_socket_is_safe($PVE_LAUNCH_SOCKET);

    my $socket = IO::Socket::UNIX->new(
        Type => SOCK_STREAM,
        Peer => $PVE_LAUNCH_SOCKET,
        Timeout => $SOCKET_TIMEOUT_SECONDS,
    );
    _unavailable() if !$socket;

    my $request = encode_json({
        version => 1,
        op => 'pve_acl_launch',
        node => $node,
        vmid => $vmid + 0,
        subject => $subject,
    }) . "\n";
    _unavailable() if length($request) > 1024;

    my $line;
    eval {
        _write_all($socket, $request);
        $line = _read_one_line($socket);
        1;
    } or _unavailable();
    close($socket);

    my $descriptor;
    eval { $descriptor = decode_json($line); 1 } or _unavailable();
    _unavailable() if !_valid_descriptor($descriptor);

    # JSON decoders retain the terminal broker's decimal port and millisecond
    # expiry as string scalars.  The browser launch-file schema deliberately
    # requires JSON numbers for both values, so turn only the fields already
    # lexically and range-validated above into numeric Perl scalars before PVE
    # serializes its ExtJS response.  Do not broadly coerce broker data.
    $descriptor->{expires_at_utc_ms} = 0 + $descriptor->{expires_at_utc_ms};
    $descriptor->{endpoint}->{port} = 0 + $descriptor->{endpoint}->{port};
    return $descriptor;
}

PVE::API2::Qemu->register_method({
    name => 'q_sunshine_launch',
    path => '{vmid}/q-sunshine',
    method => 'POST',
    protected => 1,
    proxyto => 'node',
    # Keep this deliberately identical to PVE's native console ACL policy.
    # It is evaluated by PVE before this protected handler is forwarded to the
    # root pvedaemon worker.
    permissions => {
        check => ['perm', '/vms/{vmid}', ['VM.Console']],
    },
    description => 'Create a one-use q-sunshine VM-console launch descriptor.',
    parameters => {
        additionalProperties => 0,
        properties => {
            node => get_standard_option('pve-node'),
            vmid => get_standard_option('pve-vmid'),
        },
    },
    returns => { type => 'object' },
    code => sub {
        my ($param) = @_;
        _unavailable() if $> != 0;

        my $node = $param->{node};
        my $vmid = $param->{vmid};
        _unavailable() if !defined($node) || !defined($vmid) ||
            $vmid !~ /\A[0-9]+\z/ || $vmid < $MIN_VMID || $vmid > $MAX_VMID;

        my $rpcenv = PVE::RPCEnvironment::get();
        my $subject = $rpcenv->get_user();
        _unavailable() if !defined($subject) || $subject !~ /\A[^\s\x00]{1,64}\z/;

        # Avoid turning the root-local broker into a VM-id oracle or issuing a
        # descriptor for a stopped machine. Permissions have already been
        # checked above, so these checks cannot disclose VM state to callers
        # lacking VM.Console.
        eval {
            PVE::QemuConfig->load_config($vmid, $node);
            die "not running\n" if !PVE::QemuServer::check_running($vmid);
            1;
        } or _unavailable();

        return _launch_descriptor_from_terminal($node, $vmid, $subject);
    },
});

1;
