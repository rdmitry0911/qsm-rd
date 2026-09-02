// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * Issue one short-lived q-sunshine GameStream client certificate from a CSR.
 *
 * This deliberately small helper receives only a public CSR on stdin.  Its
 * caller has already authenticated a qsa1 session ticket and supplies the
 * authenticated subject and VM audience as arguments.  No private client key
 * is ever accepted or emitted by this process.
 */

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include <openssl/bn.h>
#include <openssl/core_names.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/rand.h>
#include <openssl/x509v3.h>

enum {
  MAX_CSR_BYTES = 16 * 1024,
  MIN_LEASE_SECONDS = 60,
  MAX_LEASE_SECONDS = 900,
  MIN_CLIENT_RSA_BITS = 2048,
  MAX_CLIENT_RSA_BITS = 4096,
  MIN_CA_RSA_BITS = 3072,
  SERIAL_BYTES = 20,
  MAX_SUBJECT_LENGTH = 64,
  MAX_AUDIENCE_LENGTH = 128,
};

struct options {
  const char *ca_cert_path;
  const char *ca_key_path;
  const char *subject;
  const char *audience;
  unsigned int ttl_seconds;
  bool require_root_owner;
};

static bool is_subject_char(unsigned char character) {
  return isalnum(character) || character == '_' || character == '.' ||
         character == '@' || character == '-';
}

static bool is_audience_char(unsigned char character) {
  return isalnum(character) || character == '_' || character == '.' ||
         character == ':' || character == '-';
}

static bool valid_identifier(const char *value, size_t maximum_length,
                             bool (*valid_char)(unsigned char)) {
  size_t length;

  if (value == NULL || value[0] == '\0' || !isalnum((unsigned char) value[0])) {
    return false;
  }
  length = strnlen(value, maximum_length + 1U);
  if (length == 0U || length > maximum_length) {
    return false;
  }
  for (size_t index = 0; index < length; ++index) {
    if (!valid_char((unsigned char) value[index])) {
      return false;
    }
  }
  return true;
}

static bool parse_ttl(const char *value, unsigned int *result) {
  char *end = NULL;
  unsigned long parsed;

  if (value == NULL || value[0] == '\0') {
    return false;
  }
  for (const char *cursor = value; *cursor != '\0'; ++cursor) {
    if (!isdigit((unsigned char) *cursor)) {
      return false;
    }
  }
  errno = 0;
  parsed = strtoul(value, &end, 10);
  if (errno != 0 || end == NULL || *end != '\0' ||
      parsed < MIN_LEASE_SECONDS || parsed > MAX_LEASE_SECONDS ||
      parsed > UINT_MAX) {
    return false;
  }
  *result = (unsigned int) parsed;
  return true;
}

static bool parse_options(int argc, char **argv, struct options *options) {
  memset(options, 0, sizeof(*options));

  for (int index = 1; index < argc; ++index) {
    const char *argument = argv[index];
    if (strcmp(argument, "--require-root-owner") == 0) {
      if (options->require_root_owner) {
        return false;
      }
      options->require_root_owner = true;
      continue;
    }
    if (++index >= argc) {
      return false;
    }
    if (strcmp(argument, "--ca-cert") == 0 && options->ca_cert_path == NULL) {
      options->ca_cert_path = argv[index];
    } else if (strcmp(argument, "--ca-key") == 0 && options->ca_key_path == NULL) {
      options->ca_key_path = argv[index];
    } else if (strcmp(argument, "--subject") == 0 && options->subject == NULL) {
      options->subject = argv[index];
    } else if (strcmp(argument, "--audience") == 0 && options->audience == NULL) {
      options->audience = argv[index];
    } else if (strcmp(argument, "--ttl-seconds") == 0 && options->ttl_seconds == 0U) {
      if (!parse_ttl(argv[index], &options->ttl_seconds)) {
        return false;
      }
    } else {
      return false;
    }
  }

  return options->ca_cert_path != NULL && options->ca_key_path != NULL &&
         options->ttl_seconds != 0U &&
         valid_identifier(options->subject, MAX_SUBJECT_LENGTH, is_subject_char) &&
         valid_identifier(options->audience, MAX_AUDIENCE_LENGTH, is_audience_char);
}

static BIO *open_secure_bio(const char *path, bool private_key,
                            bool require_root_owner) {
  int descriptor = -1;
  int flags = O_RDONLY | O_CLOEXEC;
  struct stat metadata;
  BIO *bio = NULL;

#ifdef O_NOFOLLOW
  flags |= O_NOFOLLOW;
#endif
  descriptor = open(path, flags);
  if (descriptor < 0) {
    return NULL;
  }
  if (fstat(descriptor, &metadata) != 0 || !S_ISREG(metadata.st_mode) ||
      (require_root_owner && metadata.st_uid != 0) ||
      (private_key && (metadata.st_mode & 0077) != 0) ||
      (!private_key && (metadata.st_mode & 0022) != 0)) {
    close(descriptor);
    return NULL;
  }
  bio = BIO_new_fd(descriptor, BIO_CLOSE);
  if (bio == NULL) {
    close(descriptor);
  }
  return bio;
}

static bool read_csr(unsigned char **bytes, size_t *length) {
  unsigned char *buffer = NULL;
  size_t used = 0;

  buffer = calloc(MAX_CSR_BYTES + 1U, sizeof(*buffer));
  if (buffer == NULL) {
    return false;
  }
  while (used < MAX_CSR_BYTES) {
    ssize_t count = read(STDIN_FILENO, buffer + used, MAX_CSR_BYTES - used);
    if (count == 0) {
      break;
    }
    if (count < 0) {
      if (errno == EINTR) {
        continue;
      }
      free(buffer);
      return false;
    }
    used += (size_t) count;
  }
  if (used == MAX_CSR_BYTES) {
    unsigned char extra;
    ssize_t count;
    do {
      count = read(STDIN_FILENO, &extra, sizeof(extra));
    } while (count < 0 && errno == EINTR);
    if (count != 0) {
      free(buffer);
      return false;
    }
  }
  if (used == 0U || memchr(buffer, '\0', used) != NULL) {
    free(buffer);
    return false;
  }
  buffer[used] = '\0';
  *bytes = buffer;
  *length = used;
  return true;
}

static bool valid_rsa_key(EVP_PKEY *key, int minimum_bits, int maximum_bits) {
  BIGNUM *exponent = NULL;
  bool valid = false;

  if (key == NULL || EVP_PKEY_is_a(key, "RSA") != 1 ||
      EVP_PKEY_bits(key) < minimum_bits || EVP_PKEY_bits(key) > maximum_bits) {
    return false;
  }
  if (EVP_PKEY_get_bn_param(key, OSSL_PKEY_PARAM_RSA_E, &exponent) != 1 ||
      exponent == NULL) {
    return false;
  }
  valid = BN_is_word(exponent, RSA_F4) == 1;
  BN_free(exponent);
  return valid;
}

static bool valid_issuer_ca(X509 *certificate) {
  BASIC_CONSTRAINTS *constraints = NULL;
  ASN1_BIT_STRING *key_usage = NULL;
  const ASN1_TIME *not_before;
  const ASN1_TIME *not_after;
  int constraints_critical = -1;
  int key_usage_critical = -1;
  bool valid = false;

  if (certificate == NULL || X509_check_ca(certificate) <= 0) {
    return false;
  }
  constraints = X509_get_ext_d2i(certificate, NID_basic_constraints,
                                 &constraints_critical, NULL);
  key_usage = X509_get_ext_d2i(certificate, NID_key_usage,
                               &key_usage_critical, NULL);
  not_before = X509_get0_notBefore(certificate);
  not_after = X509_get0_notAfter(certificate);
  /* This endpoint does not trust legacy self-signed v1 roots or a CA without
   * an explicit, critical key-cert-signing profile. */
  valid = constraints != NULL && constraints->ca != 0 &&
          constraints_critical == 1 && key_usage != NULL &&
          key_usage_critical == 1 && ASN1_BIT_STRING_get_bit(key_usage, 5) == 1 &&
          not_before != NULL && not_after != NULL &&
          X509_cmp_current_time(not_before) < 0 &&
          X509_cmp_current_time(not_after) > 0;
  BASIC_CONSTRAINTS_free(constraints);
  ASN1_BIT_STRING_free(key_usage);
  return valid;
}

static bool add_extension(X509 *certificate, X509 *issuer, int nid,
                          const char *value) {
  X509V3_CTX context;
  X509_EXTENSION *extension;

  X509V3_set_ctx(&context, issuer, certificate, NULL, NULL, 0);
  extension = X509V3_EXT_nconf_nid(NULL, &context, nid, (char *) value);
  if (extension == NULL) {
    return false;
  }
  if (X509_add_ext(certificate, extension, -1) != 1) {
    X509_EXTENSION_free(extension);
    return false;
  }
  X509_EXTENSION_free(extension);
  return true;
}

static bool set_random_serial(X509 *certificate) {
  unsigned char raw[SERIAL_BYTES];
  BIGNUM *serial_number = NULL;
  ASN1_INTEGER *serial_asn1 = NULL;
  bool valid = false;

  if (RAND_bytes(raw, sizeof(raw)) != 1) {
    return false;
  }
  raw[0] &= 0x7fU;  // X.509 serial numbers must be non-negative.
  if (raw[0] == 0U) {
    raw[0] = 1U;
  }
  serial_number = BN_bin2bn(raw, sizeof(raw), NULL);
  if (serial_number == NULL) {
    return false;
  }
  serial_asn1 = BN_to_ASN1_INTEGER(serial_number, NULL);
  if (serial_asn1 != NULL && X509_set_serialNumber(certificate, serial_asn1) == 1) {
    valid = true;
  }
  ASN1_INTEGER_free(serial_asn1);
  BN_free(serial_number);
  return valid;
}

static X509 *issue_certificate(X509_REQ *request, X509 *ca_certificate,
                               EVP_PKEY *ca_key,
                               const struct options *options) {
  EVP_PKEY *client_key = NULL;
  X509_NAME *subject_name = NULL;
  X509 *certificate = NULL;
  char common_name[sizeof("q-sunshine user ") + MAX_SUBJECT_LENGTH];
  char subject_alt_name[sizeof("critical,URI:urn:q-sunshine:aud:") + MAX_AUDIENCE_LENGTH];
  int common_name_length;
  int subject_alt_name_length;

  client_key = X509_REQ_get_pubkey(request);
  if (X509_REQ_get_version(request) != 0L ||
      !valid_rsa_key(client_key, MIN_CLIENT_RSA_BITS, MAX_CLIENT_RSA_BITS) ||
      X509_REQ_verify(request, client_key) != 1) {
    EVP_PKEY_free(client_key);
    return NULL;
  }
  common_name_length = snprintf(common_name, sizeof(common_name),
                                "q-sunshine user %s", options->subject);
  subject_alt_name_length = snprintf(subject_alt_name, sizeof(subject_alt_name),
                                     "critical,URI:urn:q-sunshine:aud:%s",
                                     options->audience);
  if (common_name_length < 0 ||
      (size_t) common_name_length >= sizeof(common_name) ||
      subject_alt_name_length < 0 ||
      (size_t) subject_alt_name_length >= sizeof(subject_alt_name)) {
    EVP_PKEY_free(client_key);
    return NULL;
  }

  certificate = X509_new();
  subject_name = X509_NAME_new();
  if (certificate == NULL || subject_name == NULL ||
      X509_set_version(certificate, 2L) != 1 ||
      !set_random_serial(certificate) ||
      X509_gmtime_adj(X509_getm_notBefore(certificate), 0) == NULL ||
      X509_gmtime_adj(X509_getm_notAfter(certificate), options->ttl_seconds) == NULL ||
      X509_set_issuer_name(certificate, X509_get_subject_name(ca_certificate)) != 1 ||
      X509_NAME_add_entry_by_txt(subject_name, "CN", MBSTRING_ASC,
                                 (unsigned char *) common_name, -1, -1, 0) != 1 ||
      X509_set_subject_name(certificate, subject_name) != 1 ||
      X509_set_pubkey(certificate, client_key) != 1 ||
      !add_extension(certificate, ca_certificate, NID_basic_constraints,
                     "critical,CA:FALSE") ||
      !add_extension(certificate, ca_certificate, NID_key_usage,
                     "critical,digitalSignature,keyEncipherment") ||
      !add_extension(certificate, ca_certificate, NID_ext_key_usage,
                     "critical,clientAuth") ||
      !add_extension(certificate, ca_certificate, NID_subject_alt_name,
                     subject_alt_name) ||
      !add_extension(certificate, ca_certificate, NID_subject_key_identifier,
                     "hash") ||
      !add_extension(certificate, ca_certificate, NID_authority_key_identifier,
                     "keyid:always") ||
      X509_sign(certificate, ca_key, EVP_sha256()) <= 0) {
    X509_free(certificate);
    certificate = NULL;
  }
  X509_NAME_free(subject_name);
  EVP_PKEY_free(client_key);
  return certificate;
}

int main(int argc, char **argv) {
  struct options options;
  unsigned char *csr_bytes = NULL;
  size_t csr_length = 0;
  BIO *csr_bio = NULL;
  BIO *ca_cert_bio = NULL;
  BIO *ca_key_bio = NULL;
  X509_REQ *request = NULL;
  X509 *ca_certificate = NULL;
  EVP_PKEY *ca_key = NULL;
  X509 *lease_certificate = NULL;
  int result = EXIT_FAILURE;

  if (!parse_options(argc, argv, &options) ||
      !read_csr(&csr_bytes, &csr_length)) {
    goto done;
  }
  csr_bio = BIO_new_mem_buf(csr_bytes, (int) csr_length);
  ca_cert_bio = open_secure_bio(options.ca_cert_path, false, options.require_root_owner);
  ca_key_bio = open_secure_bio(options.ca_key_path, true, options.require_root_owner);
  if (csr_bio == NULL || ca_cert_bio == NULL || ca_key_bio == NULL) {
    goto done;
  }
  request = PEM_read_bio_X509_REQ(csr_bio, NULL, NULL, NULL);
  ca_certificate = PEM_read_bio_X509(ca_cert_bio, NULL, NULL, NULL);
  ca_key = PEM_read_bio_PrivateKey(ca_key_bio, NULL, NULL, NULL);
  if (request == NULL || ca_certificate == NULL || ca_key == NULL ||
      !valid_issuer_ca(ca_certificate) ||
      !valid_rsa_key(ca_key, MIN_CA_RSA_BITS, INT_MAX) ||
      X509_check_private_key(ca_certificate, ca_key) != 1) {
    goto done;
  }
  lease_certificate = issue_certificate(request, ca_certificate, ca_key, &options);
  if (lease_certificate == NULL || PEM_write_X509(stdout, lease_certificate) != 1 ||
      fflush(stdout) != 0) {
    goto done;
  }
  result = EXIT_SUCCESS;

done:
  X509_free(lease_certificate);
  EVP_PKEY_free(ca_key);
  X509_free(ca_certificate);
  X509_REQ_free(request);
  BIO_free(ca_key_bio);
  BIO_free(ca_cert_bio);
  BIO_free(csr_bio);
  if (csr_bytes != NULL) {
    OPENSSL_cleanse(csr_bytes, csr_length);
    free(csr_bytes);
  }
  return result;
}
