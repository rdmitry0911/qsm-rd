# SPDX-License-Identifier: AGPL-3.0-or-later
#
# PVE 9 protected browser-WebRTC Console route for qsm-pve-direct.
package PVE::API2::QsmDirect;

use strict;
use warnings;

use IO::Select;
use IO::Socket::UNIX qw(SOCK_STREAM);
use JSON qw(decode_json encode_json);
use Fcntl qw(S_ISSOCK O_CREAT O_EXCL O_WRONLY);

use PVE::API2::Qemu;
use PVE::JSONSchema qw(get_standard_option);
use PVE::QemuConfig;
use PVE::QemuServer;
use PVE::RPCEnvironment;

my $PVE_WEBRTC_SOCKET = '/run/qsm-pve-direct-terminal/pve-webrtc.sock';
my $SOCKET_TIMEOUT_SECONDS = 18;
my $MAX_RESPONSE_BYTES = 160 * 1024;
my $MAX_SDP_BYTES = 128 * 1024;
my $MIN_VMID = 100;
my $MAX_VMID = 999_999_999;
my $INSTANCE_DIRECTORY = '/etc/qsm-pve-direct/instances.d';

sub _unavailable {
    die "qsm direct console is unavailable\n";
}

sub _private_socket_is_safe {
    my ($path) = @_;
    my @stat = lstat($path);
    return 0 if !@stat;
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

sub _positive_integer {
    my ($value, $minimum, $maximum) = @_;
    return 0 if !defined($value) || ref($value) || $value !~ /\A[0-9]+\z/;
    return 0 if $value < $minimum || $value > $maximum;
    return 1;
}

sub _policy_defaults {
    return { codec => 'h264', encoder => 'auto' };
}

sub _policy_path {
    my ($vmid) = @_;
    _unavailable() if !_positive_integer($vmid, $MIN_VMID, $MAX_VMID);
    return "$INSTANCE_DIRECTORY/$vmid.conf";
}

sub _read_vm_policy {
    my ($vmid) = @_;
    my $path = _policy_path($vmid);
    my @stat = lstat($path);
    return _policy_defaults() if !@stat && $!{ENOENT};
    _unavailable() if !@stat || !-f _ || $stat[4] != 0 || ($stat[2] & 0o077) || $stat[7] > 16384;
    open(my $file, '<', $path) or _unavailable();
    my %values;
    while (my $line = <$file>) {
        chomp($line);
        next if $line =~ /\A\s*(?:#|\z)/;
        _unavailable() if $line !~ /\A([A-Z0-9_]+)=([^\r\n\x00]*)\z/ || exists($values{$1});
        $values{$1} = $2;
    }
    close($file) or _unavailable();
    my $codec = $values{QSM_DIRECT_CODEC} // 'h264';
    my $encoder = $values{QSM_DIRECT_ENCODER_MODE} // 'auto';
    _unavailable() if $codec ne 'h264' || $encoder !~ /\A(?:auto|hardware|software)\z/;
    return { codec => $codec, encoder => $encoder };
}

sub _write_vm_policy {
    my ($vmid, $codec, $encoder) = @_;
    _unavailable() if $codec ne 'h264' || $encoder !~ /\A(?:auto|hardware|software)\z/;
    my $path = _policy_path($vmid);
    _unavailable() if !-d $INSTANCE_DIRECTORY || !-O $INSTANCE_DIRECTORY;
    my $temporary = "$path.$$.new";
    sysopen(my $file, $temporary, O_WRONLY | O_CREAT | O_EXCL, 0600) or _unavailable();
    my $content = join('',
        "QSM_DIRECT_QEMU_DBUS_ADDRESS=unix:path=/run/qsm-pve-direct/$vmid/qemu-display1.bus\n",
        "QSM_DIRECT_CODEC=$codec\n",
        "QSM_DIRECT_ENCODER=auto\n",
        "QSM_DIRECT_ENCODER_MODE=$encoder\n",
    );
    if (!print($file $content) || !close($file) || !chmod(0600, $temporary) || !rename($temporary, $path)) {
        unlink($temporary);
        _unavailable();
    }
    return _read_vm_policy($vmid);
}

sub _valid_answer {
    my ($answer) = @_;
    return 0 if ref($answer) ne 'HASH';
    return 0 if join("\0", sort keys($answer->%*)) ne join("\0", qw(sdp type));
    return 0 if !defined($answer->{type}) || ref($answer->{type}) || $answer->{type} ne 'answer';
    return 0 if !defined($answer->{sdp}) || ref($answer->{sdp}) ||
        length($answer->{sdp}) < 1 || length($answer->{sdp}) > $MAX_SDP_BYTES ||
        $answer->{sdp} !~ /\Av=0\r?\n/ || $answer->{sdp} =~ /[^\x20-\x7e\r\n]/;
    return 1;
}

sub _answer_from_terminal {
    my ($node, $vmid, $subject, $sdp, $width, $height, $fps) = @_;
    _unavailable() if !_private_socket_is_safe($PVE_WEBRTC_SOCKET);
    my $socket = IO::Socket::UNIX->new(
        Type => SOCK_STREAM, Peer => $PVE_WEBRTC_SOCKET, Timeout => $SOCKET_TIMEOUT_SECONDS,
    );
    _unavailable() if !$socket;
    my $request = encode_json({
        version => 1, op => 'pve_acl_webrtc', node => $node, vmid => $vmid + 0,
        subject => $subject, sdp => $sdp, sdp_type => 'offer', width => $width + 0,
        height => $height + 0, fps => $fps + 0,
    }) . "\n";
    _unavailable() if length($request) > $MAX_SDP_BYTES + 4096;
    my $line;
    eval { _write_all($socket, $request); $line = _read_one_line($socket); 1 } or _unavailable();
    close($socket);
    my $response;
    eval { $response = decode_json($line); 1 } or _unavailable();
    _unavailable() if ref($response) ne 'HASH' || join("\0", sort keys($response->%*)) ne
        join("\0", qw(ok result)) || !$response->{ok} || !_valid_answer($response->{result});
    return $response->{result};
}

PVE::API2::Qemu->register_method({
    name => 'qsm_direct_webrtc',
    path => '{vmid}/qsm-direct',
    method => 'POST',
    protected => 1,
    proxyto => 'node',
    permissions => { check => ['perm', '/vms/{vmid}', ['VM.Console']] },
    description => 'Create an authorized direct browser WebRTC console session.',
    parameters => {
        additionalProperties => 0,
        properties => {
            node => get_standard_option('pve-node'),
            vmid => get_standard_option('pve-vmid'),
            sdp => { type => 'string', minLength => 1, maxLength => $MAX_SDP_BYTES },
            width => { type => 'integer', minimum => 64, maximum => 16384 },
            height => { type => 'integer', minimum => 64, maximum => 16384 },
            fps => { type => 'integer', minimum => 10, maximum => 240 },
        },
    },
    returns => { type => 'object' },
    code => sub {
        my ($param) = @_;
        _unavailable() if $> != 0;
        my ($node, $vmid, $sdp, $width, $height, $fps) = @{$param}{qw(node vmid sdp width height fps)};
        _unavailable() if !defined($node) || !defined($vmid) || !defined($sdp) ||
            $node !~ /\A[A-Za-z0-9][A-Za-z0-9.-]{0,62}\z/ ||
            !_positive_integer($vmid, $MIN_VMID, $MAX_VMID) ||
            ref($sdp) || length($sdp) > $MAX_SDP_BYTES || $sdp !~ /\Av=0\r?\n/ ||
            $sdp =~ /[^\x20-\x7e\r\n]/ ||
            !_positive_integer($width, 64, 16384) || !_positive_integer($height, 64, 16384) ||
            !_positive_integer($fps, 10, 240) || $width % 2 || $height % 2;
        my $rpcenv = PVE::RPCEnvironment::get();
        my $subject = $rpcenv->get_user();
        _unavailable() if !defined($subject) || $subject !~ /\A[^\s\x00]{1,64}\z/;
        eval {
            PVE::QemuConfig->load_config($vmid, $node);
            die "not running\n" if !PVE::QemuServer::check_running($vmid);
            1;
        } or _unavailable();
        return _answer_from_terminal($node, $vmid, $subject, $sdp, $width, $height, $fps);
    },
});

# Codec/encoder policy is VM-scoped node configuration, deliberately separate
# from PVE's unsupported QEMU config keys.  The direct WebRTC implementation
# currently exposes only H.264: common browser WebRTC implementations (and
# aiortc's server RTP stack) do not provide a portable HEVC negotiated codec.
# Keep this endpoint explicit rather than accepting HEVC and silently encoding
# the wrong stream.  A future HEVC transport can extend the enum atomically.
PVE::API2::Qemu->register_method({
    name => 'qsm_direct_settings_get',
    path => '{vmid}/qsm-direct-settings',
    method => 'GET',
    protected => 1,
    proxyto => 'node',
    permissions => { check => ['perm', '/vms/{vmid}', ['VM.Console']] },
    description => 'Read the VM-scoped QSM Direct browser codec policy.',
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
        my ($node, $vmid) = @{$param}{qw(node vmid)};
        _unavailable() if !defined($node) || $node !~ /\A[A-Za-z0-9][A-Za-z0-9.-]{0,62}\z/ ||
            !_positive_integer($vmid, $MIN_VMID, $MAX_VMID);
        return _read_vm_policy($vmid);
    },
});

PVE::API2::Qemu->register_method({
    name => 'qsm_direct_settings_set',
    path => '{vmid}/qsm-direct-settings',
    method => 'PUT',
    protected => 1,
    proxyto => 'node',
    permissions => { check => ['perm', '/vms/{vmid}', ['VM.Config.Options']] },
    description => 'Set the VM-scoped QSM Direct browser codec policy.',
    parameters => {
        additionalProperties => 0,
        properties => {
            node => get_standard_option('pve-node'),
            vmid => get_standard_option('pve-vmid'),
            codec => { type => 'string', enum => ['h264'] },
            encoder => { type => 'string', enum => ['auto', 'hardware', 'software'] },
        },
    },
    returns => { type => 'object' },
    code => sub {
        my ($param) = @_;
        _unavailable() if $> != 0;
        my ($node, $vmid, $codec, $encoder) = @{$param}{qw(node vmid codec encoder)};
        _unavailable() if !defined($node) || $node !~ /\A[A-Za-z0-9][A-Za-z0-9.-]{0,62}\z/ ||
            !_positive_integer($vmid, $MIN_VMID, $MAX_VMID) ||
            !defined($codec) || !defined($encoder);
        return _write_vm_policy($vmid, $codec, $encoder);
    },
});

1;
