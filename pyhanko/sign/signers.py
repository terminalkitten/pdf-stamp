import binascii
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Optional

import tzlocal
from asn1crypto import x509, cms, core, algos, pem, keys, pdf as asn1_pdf
from certvalidator import ValidationContext, CertificateValidator
from oscrypto import asymmetric, keys as oskeys

from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.generic import pdf_name, pdf_date, pdf_string
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.misc import BoxConstraints
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import general
from pyhanko.sign.fields import (
    enumerate_sig_fields, _prepare_sig_field,
    SigSeedValueSpec, SigSeedValFlags, SigSeedSubFilter, MDPPerm, FieldMDPSpec,
)
from pyhanko.sign.timestamps import TimeStamper
from pyhanko.sign.general import (
    simple_cms_attribute, CertificateStore,
    SimpleCertificateStore, SigningError,
)
from pyhanko.stamp import (
    TextStampStyle, TextStamp, STAMP_ART_CONTENT,
    QRStampStyle, QRStamp,
)

__all__ = ['Signer', 'SimpleSigner', 'PdfSigner', 'sign_pdf',
           'SignatureObject']


logger = logging.getLogger(__name__)


class SigByteRangeObject(generic.PdfObject):

    def __init__(self):
        self._filled = False
        self._range_object_offset = None
        self.first_region_len = 0
        self.second_region_offset = 0
        self.second_region_len = 0

    def fill_offsets(self, stream, sig_start, sig_end, eof):
        if self._filled:
            raise ValueError('Offsets already filled')
        if self._range_object_offset is None:
            raise ValueError(
                'Could not determine where to write /ByteRange value'
            )

        old_seek = stream.tell()
        self.first_region_len = sig_start
        self.second_region_offset = sig_end
        self.second_region_len = eof - sig_end
        # our ArrayObject is rigged to have fixed width
        # so we can just write over it

        stream.seek(self._range_object_offset)
        self.write_to_stream(stream, None)

        stream.seek(old_seek)

    def write_to_stream(self, stream, encryption_key):
        if self._range_object_offset is None:
            self._range_object_offset = stream.tell()
        string_repr = "[ %08d %08d %08d %08d ]" % (
            0, self.first_region_len,
            self.second_region_offset, self.second_region_len,
        )
        stream.write(string_repr.encode('ascii'))


class DERPlaceholder(generic.PdfObject):

    def __init__(self, bytes_reserved=None):
        self._placeholder = True
        self.value = b'0' * (bytes_reserved or 8192)
        self._offsets = None

    @property
    def offsets(self):
        if self._offsets is None:
            raise ValueError('No offsets available')
        return self._offsets

    @property
    def original_bytes(self):
        return self.value

    # always ignore encryption key, since this is a placeholder
    def write_to_stream(self, stream, encryption_key):
        start = stream.tell()
        stream.write(b'<')
        stream.write(self.value)
        stream.write(b'>')
        end = stream.tell()
        if self._offsets is None:
            self._offsets = start, end


class PdfSignedData(generic.DictionaryObject):
    def __init__(self, obj_type, subfilter: SigSeedSubFilter,
                 timestamp: datetime = None, bytes_reserved=None):
        if bytes_reserved is not None and bytes_reserved % 2 == 1:
            raise ValueError('bytes_reserved must be even')

        super().__init__(
            {
                pdf_name('/Type'): obj_type,
                pdf_name('/Filter'): pdf_name('/Adobe.PPKLite'),
                pdf_name('/SubFilter'): subfilter.value,
            }
        )

        if timestamp is not None:
            self[pdf_name('/M')] = pdf_date(timestamp)

        # initialise placeholders for /Contents and /ByteRange
        sig_contents = DERPlaceholder(bytes_reserved=bytes_reserved)
        self[pdf_name('/Contents')] = self.signature_contents = sig_contents
        byte_range = SigByteRangeObject()
        self[pdf_name('/ByteRange')] = self.byte_range = byte_range

    def write_signature(self, writer: IncrementalPdfFileWriter, md_algorithm,
                        in_place=False):
        if in_place:
            output = writer.prev.stream
            writer.write_in_place()
        else:
            # Render the PDF to a byte buffer with placeholder values
            # for the signature data
            output = BytesIO()
            writer.write(output)

        # retcon time: write the proper values of the /ByteRange entry
        #  in the signature object
        eof = output.tell()
        sig_start, sig_end = self.signature_contents.offsets
        self.byte_range.fill_offsets(output, sig_start, sig_end, eof)

        # compute the digests
        md = getattr(hashlib, md_algorithm)()
        if isinstance(output, BytesIO):
            output_buffer = output.getbuffer()
            # these are memoryviews, so slices should not copy stuff around
            md.update(output_buffer[:sig_start])
            md.update(output_buffer[sig_end:eof])
            output_buffer.release()
        else:
            # TODO chunk these reads
            output.seek(0)
            md.update(output.read(sig_start))
            output.seek(sig_end)
            md.update(output.read())

        digest_value = md.digest()
        signature_cms = yield digest_value

        signature_bytes = signature_cms.dump()
        signature = binascii.hexlify(signature_bytes).upper()

        # might as well compute this
        bytes_reserved = sig_end - sig_start - 2
        length = len(signature)
        assert length <= bytes_reserved, (length, bytes_reserved)

        # +1 to skip the '<'
        output.seek(sig_start + 1)
        # NOTE: the PDF spec is not completely clear on this, but
        # signature contents are NOT supposed to be encrypted.
        # Perhaps this falls under the "strings in encrypted containers"
        # denominator in § 7.6.1?
        output.write(signature)

        output.seek(0)
        padding = bytes(bytes_reserved // 2 - len(signature_bytes))
        yield output, signature_bytes + padding


class SignatureObject(PdfSignedData):

    def __init__(self, timestamp: datetime, subfilter, name=None, location=None,
                 reason=None, bytes_reserved=None):
        super().__init__(
            obj_type=pdf_name('/Sig'), subfilter=subfilter,
            timestamp=timestamp, bytes_reserved=bytes_reserved
        )

        if name:
            self[pdf_name('/Name')] = pdf_string(name)
        if location:
            self[pdf_name('/Location')] = pdf_string(location)
        if reason:
            self[pdf_name('/Reason')] = pdf_string(reason)


class DocumentTimestamp(PdfSignedData):

    def __init__(self, bytes_reserved=None):
        super().__init__(
            obj_type=pdf_name('/DocTimeStamp'),
            subfilter=SigSeedSubFilter.ETSI_RFC3161,
            bytes_reserved=bytes_reserved
        )

        # use of Name/Location/Reason is discouraged in document timestamps by
        # PAdES, so we don't set those


class Signer:
    signing_cert: x509.Certificate
    cert_registry: CertificateStore
    pkcs7_signature_mechanism: str

    def sign_raw(self, data: bytes, digest_algorithm: str, dry_run=False):
        raise NotImplementedError

    @property
    def subject_name(self):
        name: x509.Name = self.signing_cert.subject
        result = name.native['common_name']
        try:
            email = name.native['email_address']
            result = '%s <%s>' % (result, email)
        except KeyError:
            pass
        return result

    @staticmethod
    def format_revinfo(ocsp_responses: list = None, crls: list = None):

        revinfo_dict = {}
        if ocsp_responses:
            revinfo_dict['ocsp'] = ocsp_responses

        if crls:
            revinfo_dict['crl'] = crls

        if revinfo_dict:
            revinfo = asn1_pdf.RevocationInfoArchival(revinfo_dict)
            return simple_cms_attribute(
                'adobe_revocation_info_archival', revinfo
            )

        return None

    def signed_attrs(self, data_digest: bytes, timestamp: datetime = None,
                     revocation_info=None, use_pades=False):
        attrs = [
            simple_cms_attribute('content_type', 'data'),
            simple_cms_attribute('message_digest', data_digest),
            # required by PAdES
            simple_cms_attribute(
                'signing_certificate',
                general.as_signing_certificate(self.signing_cert)
            )
        ]

        # the following attributes are only meaningful in non-PAdES signatures
        #  (i.e. old school PKCS7 with Adobe-style revocation info)
        if not use_pades:
            if timestamp is not None:
                # NOTE: PAdES actually forbids this!
                st = simple_cms_attribute(
                    'signing_time',
                    cms.Time({'utc_time': core.UTCTime(timestamp)})
                )
                attrs.append(st)

            # this is not allowed under PAdES, should use DSS instead
            if revocation_info:
                attrs.append(revocation_info)

        return cms.CMSAttributes(attrs)

    def signer_info(self, digest_algorithm: str, signed_attrs, signature):
        digest_algorithm_obj = algos.DigestAlgorithm(
            {'algorithm': digest_algorithm}
        )

        signing_cert = self.signing_cert
        # build the signer info object that goes into the PKCS7 signature
        # (see RFC 2315 § 9.2)
        sig_info = cms.SignerInfo({
            'version': 'v1',
            'sid': cms.SignerIdentifier({
                'issuer_and_serial_number': cms.IssuerAndSerialNumber({
                    'issuer': signing_cert.issuer,
                    'serial_number': signing_cert.serial_number,
                })
            }),
            'digest_algorithm': digest_algorithm_obj,
            # TODO implement PSS support
            'signature_algorithm': algos.SignedDigestAlgorithm(
                {'algorithm': self.pkcs7_signature_mechanism}
            ),
            'signed_attrs': signed_attrs,
            'signature': signature
        })
        return sig_info

    def sign(self, data_digest: bytes, digest_algorithm: str,
             timestamp: datetime = None, dry_run=False,
             revocation_info=None, use_pades=False,
             timestamper=None) -> cms.ContentInfo:

        # Implementation loosely based on similar functionality in
        # https://github.com/m32/endesive/.

        # the piece of data we'll actually sign is a DER-encoded version of the
        # signed attributes of our message
        signed_attrs = self.signed_attrs(
            data_digest, timestamp, revocation_info=revocation_info,
            use_pades=use_pades
        )
        signature = self.sign_raw(
            signed_attrs.dump(), digest_algorithm.lower(), dry_run
        )

        sig_info = self.signer_info(digest_algorithm, signed_attrs, signature)

        if timestamper is not None:
            # the timestamp server needs to cross-sign our signature
            md = getattr(hashlib, digest_algorithm)()
            md.update(signature)
            if dry_run:
                ts_token = timestamper.dummy_response(digest_algorithm)
            else:
                ts_token = timestamper.timestamp(
                    md.digest(), digest_algorithm
                )
            sig_info['unsigned_attrs'] = cms.CMSAttributes(
                [simple_cms_attribute('signature_time_stamp_token', ts_token)]
            )

        digest_algorithm_obj = algos.DigestAlgorithm(
            {'algorithm': digest_algorithm}
        )

        # do not add the TS certs at this point
        certs = set(self.cert_registry)
        certs.add(self.signing_cert)
        # this is the SignedData object for our message (see RFC 2315 § 9.1)
        signed_data = {
            'version': 'v1',
            'digest_algorithms': cms.DigestAlgorithms((digest_algorithm_obj,)),
            'encap_content_info': {'content_type': 'data'},
            'certificates': certs,
            'signer_infos': [sig_info]
        }

        # time to pack up
        return cms.ContentInfo({
            'content_type': cms.ContentType('signed_data'),
            'content': cms.SignedData(signed_data)
        })


# TODO I've encountered TSAs that will spew invalid timestamps when presented
#  with a sha512 req (Adobe Reader agrees).
#  Should get to the bottom of that. In the meantime, default to sha256
DEFAULT_MD = 'sha256'


@dataclass(frozen=True)
class PdfSignatureMetadata:
    field_name: str = None
    md_algorithm: str = None
    location: str = None
    reason: str = None
    name: str = None
    certify: bool = False

    subfilter: SigSeedSubFilter = None
    embed_validation_info: bool = False
    use_pades_lta: bool = False
    timestamp_field_name: str = None
    validation_context: ValidationContext = None
    # PAdES compliance disallows this in favour of more robust timestamping
    # strategies
    include_signedtime_attr: bool = True
    # only relevant for certification
    docmdp_permissions: MDPPerm = MDPPerm.FILL_FORMS


# FIXME this function should really be called "load_certs_from_pemder" or sth.

def load_ca_chain(ca_chain_files):
    for ca_chain_file in ca_chain_files:
        with open(ca_chain_file, 'rb') as f:
            ca_chain_bytes = f.read()
        # use the pattern from the asn1crypto docs
        # to distinguish PEM/DER and read multiple certs
        # from one PEM file (if necessary)
        if pem.detect(ca_chain_bytes):
            pems = pem.unarmor(ca_chain_bytes, multiple=True)
            for type_name, _, der in pems:
                if type_name is None or type_name.lower() == 'certificate':
                    yield x509.Certificate.load(der)
                else:  # pragma: nocover
                    logger.debug(
                        f'Skipping PEM block of type {type_name} in '
                        f'{ca_chain_file}.'
                    )
        else:
            # no need to unarmor, just try to load it immediately
            yield x509.Certificate.load(ca_chain_bytes)


@dataclass
class SimpleSigner(Signer):
    signing_cert: x509.Certificate
    signing_key: keys.PrivateKeyInfo
    cert_registry: CertificateStore
    pkcs7_signature_mechanism: str = 'rsassa_pkcs1v15'

    def sign_raw(self, data: bytes, digest_algorithm: str, dry_run=False):
        return asymmetric.rsa_pkcs1v15_sign(
            asymmetric.load_private_key(self.signing_key),
            data, digest_algorithm.lower()
        )

    @classmethod
    def _load_ca_chain(cls, ca_chain_files=None):
        try:
            return set(load_ca_chain(ca_chain_files))
        except (IOError, ValueError) as e:  # pragma: nocover
            logger.error('Could not load CA chain', e)
            return None

    @classmethod
    def load_pkcs12(cls, pfx_file, ca_chain_files=None, passphrase=None):
        # TODO support MAC integrity checking?

        try:
            with open(pfx_file, 'rb') as f:
                pfx_bytes = f.read()
        except IOError as e:  # pragma: nocover
            logger.error(f'Could not open PKCS#12 file {pfx_file}.', e)
            return None

        ca_chain = cls._load_ca_chain(ca_chain_files) if ca_chain_files else []
        if ca_chain is None:  # pragma: nocover
            return None

        (kinfo, cert, other_certs) = oskeys.parse_pkcs12(pfx_bytes, passphrase)
        cs = SimpleCertificateStore()
        cs.register_multiple(ca_chain + other_certs)
        return SimpleSigner(
            signing_key=kinfo, signing_cert=cert,
            cert_registry=cs
        )

    @classmethod
    def load(cls, key_file, cert_file, ca_chain_files=None,
             key_passphrase=None, other_certs=None):
        try:
            # load cryptographic data (both PEM and DER are supported)
            with open(key_file, 'rb') as f:
                signing_key: keys.PrivateKeyInfo = oskeys.parse_private(
                    f.read(), password=key_passphrase
                )
            with open(cert_file, 'rb') as f:
                signing_cert: x509.Certificate = oskeys.parse_certificate(
                    f.read()
                )
        except (IOError, ValueError) as e:  # pragma: nocover
            logger.error('Could not load cryptographic material', e)
            return None

        ca_chain = cls._load_ca_chain(ca_chain_files) if ca_chain_files else []
        if ca_chain is None:  # pragma: nocover
            return None

        other_certs = ca_chain if other_certs is None \
            else ca_chain + other_certs

        cert_reg = SimpleCertificateStore()
        cert_reg.register_multiple(other_certs)
        return SimpleSigner(
            signing_cert=signing_cert, signing_key=signing_key,
            cert_registry=cert_reg
        )


def docmdp_reference_dictionary(md_algorithm, permission_level: MDPPerm):
    # this is part of the /Reference entry of the signature object.
    return generic.DictionaryObject({
        pdf_name('/Type'): pdf_name('/SigRef'),
        pdf_name('/TransformMethod'): pdf_name('/DocMDP'),
        pdf_name('/DigestMethod'): pdf_name('/' + md_algorithm.upper()),
        pdf_name('/TransformParams'): generic.DictionaryObject({
            pdf_name('/Type'): pdf_name('/TransformParams'),
            pdf_name('/V'): pdf_name('/1.2'),
            pdf_name('/P'): generic.NumberObject(permission_level.value)
        })
    })


def fieldmdp_reference_dictionary(field_mdp_spec: FieldMDPSpec,
                                  md_algorithm: str,
                                  data_ref: generic.IndirectObject):
    # this is part of the /Reference entry of the signature object.
    return generic.DictionaryObject({
        pdf_name('/Type'): pdf_name('/SigRef'),
        pdf_name('/TransformMethod'): pdf_name('/FieldMDP'),
        pdf_name('/Data'): data_ref,
        pdf_name('/DigestMethod'): pdf_name('/' + md_algorithm.upper()),
        pdf_name('/TransformParams'): field_mdp_spec.as_transform_params()
    })


SIG_DETAILS_DEFAULT_TEMPLATE = (
    'Digitally signed by %(signer)s.\n'
    'Timestamp: %(ts)s.'
)


def sign_pdf(pdf_out: IncrementalPdfFileWriter,
             signature_meta: PdfSignatureMetadata, signer: Signer,
             timestamper: TimeStamper = None,
             existing_fields_only=False, bytes_reserved=None, in_place=False):
    return PdfSigner(signature_meta, signer, timestamper).sign_pdf(
        pdf_out, existing_fields_only=existing_fields_only,
        bytes_reserved=bytes_reserved, in_place=in_place
    )


# Wrapper around _prepare_sig_fields with some error reporting

def _get_or_create_sigfield(field_name, pdf_out, existing_fields_only,
                            is_timestamp):
    root = pdf_out.root
    # for feedback reasons
    if is_timestamp:
        attribute = 'timestamp_field_name'
    else:
        attribute = 'field_name'
    if field_name is None:
        if not existing_fields_only:
            raise SigningError(
                'Not specifying %s is only allowed '
                'when existing_fields_only=True' % attribute
            )

        # most of the logic in _prepare_sig_field has to do with preparing
        # for the potential addition of a new field. That is completely
        # irrelevant in this special case, so we might as well short circuit
        # things.
        field_created = False
        empty_fields = enumerate_sig_fields(
            pdf_out.prev, filled_status=False
        )
        try:
            found_field_name, _, sig_field_ref = next(empty_fields)
        except StopIteration:
            raise SigningError('There are no empty signature fields.')

        others = ', '.join(
            fn for fn, _, _ in empty_fields if fn is not None
        )
        if others:
            raise SigningError(
                'There are several empty signature fields. Please specify '
                '%s. The options are %s, %s.' % (
                    attribute, found_field_name, others
                )
            )
    else:
        # grab or create a sig field
        field_created, sig_field_ref = _prepare_sig_field(
            field_name, root, update_writer=pdf_out,
            existing_fields_only=existing_fields_only,
            lock_sig_flags=True
        )

    return field_created, sig_field_ref


class PdfTimestamper:

    def __init__(self, timestamper: TimeStamper):
        self.default_timestamper = timestamper

    def generate_timestmp_field_name(self):
        return 'Timestamp-' + str(uuid.uuid4())

    def timestamp_pdf(self, pdf_out: IncrementalPdfFileWriter,
                      md_algorithm, validation_context, bytes_reserved=None,
                      validation_paths=None, in_place=False, timestamper=None):
        timestamper = timestamper or self.default_timestamper
        field_name = self.generate_timestmp_field_name()
        if bytes_reserved is None:
            test_signature_cms = timestamper.dummy_response(md_algorithm)
            test_len = len(test_signature_cms.dump()) * 2
            # see sign_pdf comments
            bytes_reserved = test_len + 2 * (test_len // 4)

        if validation_paths is None:
            validation_paths = list(
                timestamper.validation_paths(validation_context)
            )

        timestamp_obj = DocumentTimestamp(bytes_reserved=bytes_reserved)
        field_created, sig_field_ref = _get_or_create_sigfield(
            field_name, pdf_out,
            # for LTA, requiring existing_fields_only doesn't make sense
            # since we should in principle be able to add document timestamps
            # ad infinitum.
            existing_fields_only=False, is_timestamp=True
        )
        sig_field = sig_field_ref.get_object()
        timestamp_obj_ref = pdf_out.add_object(timestamp_obj)
        sig_field[pdf_name('/V')] = timestamp_obj_ref
        # this update is unnecessary in the vast majority of cases, but
        #  let's do it anyway for consistency.
        if not field_created:  # pragma: nocover
            pdf_out.mark_update(timestamp_obj_ref)

        wr = timestamp_obj.write_signature(
            pdf_out, md_algorithm, in_place=in_place
        )
        true_digest = next(wr)
        timestamp_cms = timestamper.timestamp(true_digest, md_algorithm)
        output, sig_contents = wr.send(timestamp_cms)

        # update the DSS
        from pyhanko.sign import validation
        validation.DocumentSecurityStore.add_dss(
            output_stream=output, sig_contents=sig_contents,
            paths=validation_paths, validation_context=validation_context
        )

        return output

    def update_archival_timestamp_chain(self, reader: PdfFileReader,
                                        validation_context, in_place=True):
        # In principle, we only have to validate that the last timestamp token
        # in the current chain is valid.
        # TODO: add an option to validate the entire timestamp chain
        #  plus all signatures
        from .validation import (
            EmbeddedPdfSignature, _establish_timestamp_trust,
            DocumentSecurityStore
        )

        all_signatures = reader.embedded_signatures
        if not all_signatures:
            raise SigningError("No signatures found.")
        last_signature: EmbeddedPdfSignature = \
            all_signatures[len(all_signatures) - 1]
        last_signature.compute_digest()

        # two cases: either the signature is a normal signature,
        # or it is a document timestamp
        if last_signature.sig_object['/Type'] == '/DocTimeStamp':
            tst_token = last_signature.signed_data
            expected_imprint = last_signature.raw_digest
        else:
            # normal signature
            tst_token = last_signature.external_timestamp_data
            if tst_token is None:  # pragma: nocover
                raise SigningError("Final signature does not have a timestamp.")
            expected_imprint = last_signature.tst_signature_digest

        # run validation logic
        tst_status = _establish_timestamp_trust(
            tst_token, validation_context, expected_imprint
        )

        if in_place:
            output = reader.stream
        else:  # pragma: nocover
            output = BytesIO()
            output.write(reader.stream.read())

        # update the DSS
        DocumentSecurityStore.add_dss(
            output, last_signature.pkcs7_content,
            paths=(tst_status.validation_path,),
            validation_context=validation_context
        )

        # append a new timestamp
        return self.timestamp_pdf(
            IncrementalPdfFileWriter(output), tst_status.md_algorithm,
            validation_context, in_place=True
        )


DEFAULT_SIGNING_STAMP_STYLE = TextStampStyle(
    stamp_text=SIG_DETAILS_DEFAULT_TEMPLATE, background=STAMP_ART_CONTENT
)


class PdfSigner(PdfTimestamper):
    _ignore_sv = False

    def __init__(self, signature_meta: PdfSignatureMetadata, signer: Signer,
                 timestamper: TimeStamper = None, stamp_style=None,
                 qr_url=None):
        self.signature_meta = signature_meta
        self.signer = signer
        stamp_style = stamp_style or DEFAULT_SIGNING_STAMP_STYLE
        self.stamp_style: TextStampStyle = stamp_style

        if qr_url is not None and not isinstance(stamp_style, QRStampStyle):
            raise ValueError(
                "The qr_url parameter requires a QRStampStyle."
            )
        self.qr_url = qr_url
        super().__init__(timestamper)

    def generate_timestmp_field_name(self):
        return self.signature_meta.timestamp_field_name or (
            super().generate_timestmp_field_name()
        )

    def _apply_locking_rules(self, sig_field, sig_obj_ref, md_algorithm,
                             pdf_out):
        # this helper method handles /Lock dictionary and certification
        #  semantics.

        lock = None
        docmdp_perms = None
        try:
            lock_dict = sig_field['/Lock']
            lock = FieldMDPSpec.from_pdf_object(lock_dict)
            docmdp_value = lock_dict['/P']
            docmdp_perms = MDPPerm(docmdp_value)
        except KeyError:
            pass
        except ValueError as e:
            raise SigningError("Failed to read /Lock dictionary", e)

        if self.signature_meta.certify:
            meta_perms = self.signature_meta.docmdp_permissions
            assert meta_perms is not None
            # choose the stricter option if both are available
            docmdp_perms = meta_perms if docmdp_perms is None else (
                min(docmdp_perms, meta_perms)
            )

            # To make a certification signature, we need to leave a record
            #  in the document catalog.
            root = pdf_out.root
            try:
                perms = root['/Perms']
            except KeyError:
                root['/Perms'] = perms = generic.DictionaryObject()
            perms[pdf_name('/DocMDP')] = sig_obj_ref
            pdf_out.update_container(perms)

        reference_array = generic.ArrayObject()
        if lock is not None:
            reference_array.append(
                fieldmdp_reference_dictionary(
                    lock, md_algorithm, data_ref=pdf_out.root_ref
                )
            )

        if docmdp_perms is not None:
            reference_array.append(
                docmdp_reference_dictionary(md_algorithm, docmdp_perms)
            )

        sig_obj_ref.get_object()['/Reference'] = reference_array

    def _sig_field_appearance(self, sig_field, pdf_out, timestamp):

        name = self.signature_meta.name
        if name is None:
            name = self.signer.subject_name
        x1, y1, x2, y2 = sig_field[pdf_name('/Rect')]
        w = abs(x1 - x2)
        h = abs(y1 - y2)
        if w and h:
            # the field is probably a visible one, so we change its appearance
            # stream to show some data about the signature
            text_params = {
                'signer': name, 'ts': timestamp.strftime(
                    self.stamp_style.timestamp_format
                )
            }
            box = BoxConstraints(width=w, height=h)
            if self.qr_url is None:
                stamp = TextStamp(
                    pdf_out, style=self.stamp_style, text_params=text_params,
                    box=box
                )
            else:
                assert isinstance(self.stamp_style, QRStampStyle)
                stamp = QRStamp(
                    pdf_out, style=self.stamp_style, url=self.qr_url,
                    text_params=text_params, box=box
                )
            sig_field[pdf_name('/AP')] = stamp.as_appearances().as_pdf_object()
            try:
                # if there was an entry like this, it's meaningless now
                del sig_field[pdf_name('/AS')]
            except KeyError:
                pass

    def _enforce_certification_constraints(self, reader: PdfFileReader):
        # TODO we really should take into account the /DocMDP constraints
        #  of _all_ previous signatures

        from .validation import read_certification_data
        cd = read_certification_data(reader)
        # if there is no author signature, we don't have to do anything
        if cd is None:
            return
        if self.signature_meta.certify:
            raise SigningError(
                "Document already contains a certification signature"
            )
        if cd.permission_bits == MDPPerm.NO_CHANGES:
            raise SigningError("Author signature forbids all changes")
        author_sig_md = cd.author_sig.get('/DigestAlgorithm', None)
        if author_sig_md is not None:
            return author_sig_md
        return None

    def _enforce_seed_value_constraints(self, sig_field, validation_path) \
            -> Optional[SigSeedValueSpec]:
        # for testing & debugging
        if self._ignore_sv:
            return None

        sv_dict = sig_field.get('/SV')
        if sv_dict is None:
            return None
        sv_spec: SigSeedValueSpec = SigSeedValueSpec.from_pdf_object(sv_dict)
        flags: SigSeedValFlags = sv_spec.flags

        if sv_spec.cert is not None:
            sv_spec.cert.satisfied_by(self.signer.signing_cert, validation_path)

        if not flags:
            return sv_spec

        if flags & SigSeedValFlags.UNSUPPORTED:
            raise NotImplementedError(
                "Unsupported mandatory seed value items: " + repr(
                    flags & SigSeedValFlags.UNSUPPORTED
                )
            )
        selected_sf = self.signature_meta.subfilter
        if (flags & SigSeedValFlags.SUBFILTER) \
                and sv_spec.subfilters is not None:
            # empty array = no supported subfilters
            if not sv_spec.subfilters:
                raise NotImplementedError(
                    "The signature encodings mandated by the seed value "
                    "dictionary are not supported."
                )
            # standard mandates that we take the first available subfilter
            mandated_sf: SigSeedSubFilter = sv_spec.subfilters[0]
            if selected_sf is not None and mandated_sf != selected_sf:
                raise SigningError(
                    "The seed value dictionary mandates subfilter '%s', "
                    "but '%s' was requested." % (
                        mandated_sf.value, selected_sf.value
                    )
                )

        # SV dict serves as a source of defaults as well
        if selected_sf is None and sv_spec.subfilters is not None:
            selected_sf = sv_spec.subfilters[0]

        if (flags & SigSeedValFlags.ADD_REV_INFO) \
                and sv_spec.add_rev_info is not None:
            if sv_spec.add_rev_info != \
                    self.signature_meta.embed_validation_info:
                raise SigningError(
                    "The seed value dict mandates that revocation info %sbe "
                    "added; adjust PdfSignatureMetadata settings accordingly."
                    % ("" if sv_spec.add_rev_info else "not ")
                )
            if sv_spec.add_rev_info and \
                    selected_sf != SigSeedSubFilter.ADOBE_PKCS7_DETACHED:
                raise SigningError(
                    "The seed value dict mandates that Adobe-style revocation "
                    "info be added; this requires subfilter '%s'" % (
                        SigSeedSubFilter.ADOBE_PKCS7_DETACHED.value
                    )
                )
        if (flags & SigSeedValFlags.DIGEST_METHOD) \
                and sv_spec.digest_methods is not None:
            selected_md = self.signature_meta.md_algorithm
            if selected_md is not None:
                selected_md = selected_md.lower()
                if selected_md not in sv_spec.digest_methods:
                    raise SigningError(
                        "The selected message digest %s is not allowed by the "
                        "seed value dictionary. Please select one of %s."
                        % (selected_md, ", ".join(sv_spec.digest_methods))
                    )

        if flags & SigSeedValFlags.REASONS:
            # standard says that omission of the /Reasons key amounts to
            #  a prohibition in this case
            must_omit = not sv_spec.reasons or sv_spec.reasons == ["."]
            reason_given = self.signature_meta.reason
            if must_omit and reason_given is not None:
                raise SigningError(
                    "The seed value dictionary prohibits giving a reason "
                    "for signing."
                )
            if not must_omit and reason_given not in sv_spec.reasons:
                raise SigningError(
                    "Reason \"%s\" is not a valid reason for signing, "
                    "please choose one of the following: %s." % (
                        reason_given,
                        ", ".join("\"%s\"" % s for s in sv_spec.reasons)
                    )
                )

        return sv_spec

    def sign_pdf(self, pdf_out: IncrementalPdfFileWriter,
                 existing_fields_only=False, bytes_reserved=None,
                 in_place=False):

        timestamper = self.default_timestamper

        # TODO if PAdES is requested, set the ESIC extension to the proper value

        timestamp = datetime.now(tz=tzlocal.get_localzone())
        signature_meta: PdfSignatureMetadata = self.signature_meta
        signer: Signer = self.signer
        validation_context = signature_meta.validation_context
        if signature_meta.embed_validation_info and validation_context is None:
            raise SigningError(
                'A validation context must be provided if '
                'validation/revocation info is to be embedded into the '
                'signature.'
            )
        validation_paths = []
        signer_cert_validation_path = None
        if validation_context is not None:
            # validate cert
            # (this also keeps track of any validation data automagically)
            validator = CertificateValidator(
                signer.signing_cert, intermediate_certs=signer.cert_registry,
                validation_context=validation_context
            )
            # TODO allow customisation of key usage parameters
            signer_cert_validation_path = validator.validate_usage(
                {"non_repudiation"}
            )
            validation_paths.append(signer_cert_validation_path)

        field_created, sig_field_ref = _get_or_create_sigfield(
            signature_meta.field_name, pdf_out,
            existing_fields_only, is_timestamp=False
        )

        sig_field = sig_field_ref.get_object()

        # process the signature's seed value dictionary
        sv_spec = self._enforce_seed_value_constraints(
            sig_field, signer_cert_validation_path
        )

        author_sig_md_algorithm = self._enforce_certification_constraints(
            pdf_out.prev
        )
        # priority order for the message digest algorithm
        #  (1) If signature_meta specifies a message digest algorithm, use it
        #      (it has been cleared by the SV dictionary checker already)
        #  (2) Use the first algorithm specified in the seed value dictionary,
        #      if a suggestion is present
        #  (3) If there is a certification signature, use the digest method
        #      specified there.
        #  (4) fall back to DEFAULT_MD
        if sv_spec is not None and sv_spec.digest_methods:
            sv_md_algorithm = sv_spec.digest_methods[0]
        else:
            sv_md_algorithm = None

        if self.signature_meta.md_algorithm is not None:
            md_algorithm = self.signature_meta.md_algorithm
        elif sv_md_algorithm is not None:
            md_algorithm = sv_md_algorithm
        elif author_sig_md_algorithm is not None:
            md_algorithm = author_sig_md_algorithm
        else:
            md_algorithm = DEFAULT_MD

        # same for the subfilter: try signature_meta and SV dict, fall back
        #  to /adbe.pkcs7.detached by default
        subfilter = signature_meta.subfilter
        if subfilter is None:
            if sv_spec is not None and sv_spec.subfilters:
                subfilter = sv_spec.subfilters[0]
            else:
                subfilter = SigSeedSubFilter.ADOBE_PKCS7_DETACHED
        use_pades = subfilter == SigSeedSubFilter.PADES

        ts_validation_paths = None
        ts_required = sv_spec is not None and sv_spec.timestamp_required
        if ts_required and timestamper is None:
            timestamper = sv_spec.build_timestamper()

        if timestamper is not None:
            # this might hit the TS server, but the response is cached
            # and it collects the certificates we need to verify the TS response
            timestamper.dummy_response(md_algorithm)
            if validation_context is not None:
                ts_validation_paths = list(
                    timestamper.validation_paths(validation_context)
                )
                validation_paths += ts_validation_paths

        # do we need adobe-style revocation info?
        if signature_meta.embed_validation_info and not use_pades:
            revinfo = Signer.format_revinfo(
                ocsp_responses=validation_context.ocsps,
                crls=validation_context.crls
            )
        else:
            # PAdES prescribes another mechanism for embedding revocation info
            revinfo = None

        if bytes_reserved is None:
            test_md = getattr(hashlib, md_algorithm)().digest()
            test_signature_cms = signer.sign(
                test_md, md_algorithm,
                timestamp=timestamp, use_pades=use_pades,
                dry_run=True, revocation_info=revinfo,
                timestamper=timestamper
            )
            test_len = len(test_signature_cms.dump()) * 2
            # External actors such as timestamping servers can't be relied on to
            # always return exactly the same response, so we build in a 50%
            # error margin (+ ensure that bytes_reserved is even)
            bytes_reserved = test_len + 2 * (test_len // 4)

        # we need to add a signature object and a corresponding form field
        # to the PDF file
        # Here, we pass in the name as specified in the signature metadata.
        # When it's None, the reader will/should derive it from the contents
        # of the certificate.
        sig_obj = SignatureObject(
            timestamp, name=signature_meta.name,
            location=signature_meta.location,
            reason=signature_meta.reason, bytes_reserved=bytes_reserved,
            subfilter=subfilter
        )
        sig_obj_ref = pdf_out.add_object(sig_obj)

        # fill in a reference to the (empty) signature object
        sig_field[pdf_name('/V')] = sig_obj_ref

        if not field_created:
            # still need to mark it for updating
            pdf_out.mark_update(sig_field_ref)

        # take care of the field's visual appearance (if applicable)
        self._sig_field_appearance(sig_field, pdf_out, timestamp)

        self._apply_locking_rules(sig_field, sig_obj_ref, md_algorithm, pdf_out)

        wr = sig_obj.write_signature(pdf_out, md_algorithm, in_place=in_place)
        true_digest = next(wr)

        signature_cms = signer.sign(
            true_digest, md_algorithm,
            timestamp=timestamp, use_pades=use_pades,
            revocation_info=revinfo, timestamper=timestamper
        )
        output, sig_contents = wr.send(signature_cms)

        if use_pades and signature_meta.embed_validation_info:
            from pyhanko.sign import validation
            validation.DocumentSecurityStore.add_dss(
                output_stream=output, sig_contents=sig_contents,
                paths=validation_paths, validation_context=validation_context
            )

            if timestamper is not None and signature_meta.use_pades_lta:
                # append an LTV document timestamp
                w = IncrementalPdfFileWriter(output)
                self.timestamp_pdf(
                    w, md_algorithm, validation_context,
                    validation_paths=ts_validation_paths, in_place=True,
                    timestamper=timestamper
                )

        return output
