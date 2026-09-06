# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Deliberately narrow allow-list for the private PVE API2 registration shim.
#
# PVE has no supported API-route plug-in ABI.  Loading an extra API2 module in
# pveproxy/pvedaemon is therefore safe only for the exact PVE files on which it
# was qualified.  This module is evaluated *before* PVE::API2::QsmDirect is
# loaded; a mismatch makes the package-owned launchers exec their untouched
# vendor counterparts instead of risking either PVE service.
package PVE::QsmDirect::Compatibility;

use strict;
use warnings;

use Digest::SHA ();
use Fcntl qw(S_ISREG);

# Keep this table small and reviewable.  A normal PVE package upgrade changes
# either a package version or one of these digests, which deliberately disables
# the custom route until that PVE release has been qualified and added here.
my %PROFILE = (
    packages => {
        'pve-manager' => '9.2.11',
        'qemu-server' => '9.2.7',
    },
    common_files => {
        '/usr/share/perl5/PVE/API2/Qemu.pm' =>
            '33c9b0dca650341e76e39c85c8d54ba1ee38474266632a26c8921121f055697f',
        '/usr/share/perl5/PVE/QemuConfig.pm' =>
            '4e437fad79b04bd0b3dc6b1450e7b70ff60de4d3d7d7380e308852e0a874d0c8',
        '/usr/share/perl5/PVE/QemuServer.pm' =>
            '2a0b793e6eddcd1496695cd1ec9327da71110848a98d516d29392bab7bd3df2d',
        '/usr/share/perl5/PVE/RPCEnvironment.pm' =>
            '36474324b346ef285a846df36fef936418231e8143413b509ae977d4dad4f55f',
        '/usr/share/perl5/PVE/JSONSchema.pm' =>
            'cb185fa1efa5554d392cab80009d09d376d52145a2636c55b6b9da7b291411ce',
    },
    roles => {
        pveproxy => {
            files => {
                '/usr/bin/pveproxy' =>
                    'cc48cd14900da8e5534b5a4398736dbd4af95519acbfe94d8cb8605dc53882c0',
                '/usr/share/perl5/PVE/Service/pveproxy.pm' =>
                    '4c8aa7dacaeead62a536d15fcc57202ed6e942ac27b0db41ea6029aafb66edda',
            },
        },
        pvedaemon => {
            files => {
                '/usr/bin/pvedaemon' =>
                    'a7203a04f2308784be000b7f656e963d62a97809efbd0679b747cfb2626ca669',
                '/usr/share/perl5/PVE/Service/pvedaemon.pm' =>
                    'a83e2fa203f5b3d760b3cd008b47b41d9b7b8d9b87f3c4e29cdc3dd564d5a745',
            },
        },
        pvesh => {
            files => {
                '/usr/bin/pvesh' =>
                    '9ed13d4c2d82e7864ff0192c428da7669af263f81dfcc84d5a981ca98fee8439',
                '/usr/share/perl5/PVE/CLI/pvesh.pm' =>
                    '121376be9e329ca65205367570d05ab0abfbda2f624c4168ac40d32a036396ce',
            },
        },
    },
);

my %STOCK_LAUNCHER = (
    pveproxy => '/usr/bin/pveproxy',
    pvedaemon => '/usr/bin/pvedaemon',
    pvesh => '/usr/bin/pvesh',
);

sub _package_version {
    my ($package) = @_;
    return undef if !defined($package) || $package !~ /\A[a-z0-9][a-z0-9+.-]*\z/;

    # Reading dpkg's local status database avoids executing a helper from a
    # privileged -T process.  The parser intentionally recognizes only the
    # three exact fields needed for an installed binary package.
    open(my $status_file, '<', '/var/lib/dpkg/status') or return undef;
    my %fields;
    while (my $line = <$status_file>) {
        chomp($line);
        if ($line eq '') {
            if (($fields{package} // '') eq $package &&
                ($fields{status} // '') eq 'install ok installed') {
                close($status_file);
                my $version = $fields{version};
                return undef if !defined($version) ||
                    $version !~ /\A[0-9A-Za-z.+:~_-]{1,128}\z/;
                return $version;
            }
            %fields = ();
            next;
        }
        next if $line =~ /\A[ \t]/;
        if ($line =~ /\A(Package|Status|Version):[ \t]*(.*)\z/) {
            $fields{lc($1)} = $2;
        }
    }
    close($status_file);
    return undef;
}

sub _regular_root_owned_sha256 {
    my ($path) = @_;
    my @metadata = lstat($path);
    return undef if !@metadata;
    return undef if !S_ISREG($metadata[2]);
    return undef if $metadata[4] != 0;
    return undef if $metadata[2] & 0o022;
    # These are Perl modules and small launch scripts.  A size cap makes an
    # accidental replacement with a special build artefact fail closed before
    # reading it into a privileged daemon's startup path.
    return undef if $metadata[7] > 32 * 1024 * 1024;

    open(my $file, '<', $path) or return undef;
    binmode($file);
    my $sha = Digest::SHA->new(256);
    while (1) {
        my $chunk = '';
        my $read = read($file, $chunk, 64 * 1024);
        if (!defined($read)) {
            close($file);
            return undef;
        }
        last if $read == 0;
        $sha->add($chunk);
    }
    close($file) or return undef;
    return $sha->hexdigest;
}

sub supported {
    my ($class, $role) = @_;
    return (0, 'unknown qsm-pve-direct PVE launcher')
        if !defined($role) || !exists($PROFILE{roles}{$role});
    return (0, 'requires effective root') if $> != 0;

    for my $package (sort keys($PROFILE{packages}->%*)) {
        my $actual = _package_version($package);
        return (0, "unsupported $package version")
            if !defined($actual) || $actual ne $PROFILE{packages}{$package};
    }

    my %expected = (
        $PROFILE{common_files}->%*,
        $PROFILE{roles}{$role}{files}->%*,
    );
    for my $path (sort keys(%expected)) {
        my $actual = _regular_root_owned_sha256($path);
        return (0, 'unsupported PVE file set')
            if !defined($actual) || $actual ne $expected{$path};
    }

    return (1, 'qualified PVE 9.2.11 ABI');
}

sub _safe_stock_argv {
    my ($role, @arguments) = @_;
    if ($role eq 'pveproxy' || $role eq 'pvedaemon') {
        return undef if @arguments != 1;
        my ($operation) = $arguments[0] =~ /\A(start|stop|restart|status)\z/;
        return defined($operation) ? ($operation) : undef;
    }

    # pvesh is only a root diagnostic convenience.  Permit its normal API
    # verbs, options, paths, and scalar values without forwarding a tainted
    # arbitrary argument through a -T launcher.
    my @safe;
    for my $argument (@arguments) {
        my ($clean) = $argument =~ m{\A([A-Za-z0-9][A-Za-z0-9._@%+=,/:?&-]{0,1023})\z};
        return undef if !defined($clean);
        push @safe, $clean;
    }
    return @safe;
}

sub exec_stock {
    my ($class, $role, $reason, @arguments) = @_;
    my $stock = $STOCK_LAUNCHER{$role};
    die "qsm-pve-direct PVE API launcher has no stock fallback\n" if !defined($stock);
    my @safe_arguments = _safe_stock_argv($role, @arguments);
    die "qsm-pve-direct PVE API launcher refused unsafe stock arguments\n"
        if !defined($safe_arguments[0]) && @arguments;

    # The reason originates in `supported`'s fixed messages.  Keep the journal
    # useful but do not report PVE paths, package-query output, or a dynamic
    # loader exception to a browser/API client.
    $reason = 'unsupported PVE environment' if !defined($reason) ||
        $reason !~ /\A[A-Za-z0-9 ._-]{1,128}\z/;
    print STDERR "qsm-pve-direct PVE API disabled: $reason; executing stock $role\n";

    my ($safe_stock) = $stock =~ m{\A(/usr/bin/(?:pveproxy|pvedaemon|pvesh))\z};
    die "qsm-pve-direct PVE API launcher has an invalid stock fallback\n" if !defined($safe_stock);
    local $ENV{'PATH'} = '/usr/sbin:/usr/bin:/sbin:/bin';
    exec {$safe_stock} $safe_stock, @safe_arguments;
    die "qsm-pve-direct PVE API stock fallback failed: $!\n";
}

1;
