import os
from typing import Union

from . import generic

from .reader import PdfFileReader
from .generic import pdf_name
from .content import PdfContent
from .writer import BasePdfFileWriter

"""
Utility class for writing incremental updates to PDF files.
Contains code from the PyPDF2 project, see LICENSE.PyPDF2
"""

__all__ = ['IncrementalPdfFileWriter']


class IncrementalPdfFileWriter(BasePdfFileWriter):

    def __init__(self, input_stream):
        self.input_stream = input_stream
        self.prev = prev = PdfFileReader(input_stream)
        self.trailer = trailer = prev.trailer
        root_ref = trailer.raw_get('/Root')
        try:
            info_ref = trailer.raw_get('/Info')
        except KeyError:
            # rare, but it can happen. /Info is not a required entry
            info_ref = None
        document_id = self.__class__._handle_id(prev)
        super().__init__(
            root_ref, info_ref, document_id, obj_id_start=trailer['/Size'],
            stream_xrefs=prev.has_xref_stream
        )
        self._resolves_objs_from = (self, prev)
        if self.prev.input_version != self.output_version:
            root = root_ref.get_object()
            version_str = pdf_name('/%d.%d' % self.output_version)
            root[pdf_name('/Version')] = version_str
            self.update_root()

    @classmethod
    def _handle_id(cls, prev):
        # There are a number of issues at play here
        #  - Documents *should* have a unique id, but it's not a strict
        #    requirement unless the document is encrypted.
        #  - We are updating an existing document, but the result is not the
        #    same document. Hence, we want to assign an ID to this document that
        #    is not the same as the one on the existing document.
        #  - The first part of the ID is part of the key derivation used to
        #    to encrypt documents. Since we need to encrypt the file using
        #    the same cryptographic data as the original, we cannot change
        #    this value if it is present (cf. § 7.6.3.3 in ISO 32000).
        #    Even when no encryption is involved, changing this part violates
        #    the spec (cf. § 14.4 in loc. cit.)

        # noinspection PyArgumentList
        id2 = generic.ByteStringObject(os.urandom(16))
        try:
            id1, _ = prev.trailer["/ID"]
            # is this a bug in PyPDF2?
            if isinstance(id1, generic.TextStringObject):
                # noinspection PyArgumentList
                id1 = generic.ByteStringObject(id1.original_bytes)
        except KeyError:
            # no primary ID present, so generate one
            # noinspection PyArgumentList
            id1 = generic.ByteStringObject(os.urandom(16))
        return generic.ArrayObject([id1, id2])

    def get_object(self, ido):
        try:
            return super().get_object(ido)
        except KeyError:
            return self.prev.get_object(ido)

    def mark_update(self, obj_ref: Union[generic.Reference,
                                         generic.IndirectObject]):
        ix = (obj_ref.generation, obj_ref.idnum)
        self.objects[ix] = obj_ref.get_object()

    # TODO: this new API allows me to simplify a lot of bookkeeping
    #  in the library
    def update_container(self, obj: generic.PdfObject):
        container_ref = obj.container_ref
        if container_ref is None:
            # this means that in all likelihood, the object was added by this
            # writer, and is therefore about to be written anyway.
            return
        if isinstance(container_ref, generic.TrailerReference):
            # nothing to do, the trailer is always written
            return
        elif isinstance(container_ref, generic.Reference):  # pragma: nocover
            self.mark_update(container_ref)
        else:
            raise ValueError(
                f'Cannot use {container_ref} as an update reference.'
            )

    def update_root(self):
        self.mark_update(self._root)

    def _write_header(self, stream):
        # copy the original data to the output
        input_pos = self.input_stream.tell()
        self.input_stream.seek(0)
        # TODO there has to be a better way to do this that doesn't involve
        #  loading the entire file into a separate buffer
        stream.write(self.input_stream.read())
        self.input_stream.seek(input_pos)

    def _populate_trailer(self, trailer):
        trailer.update(self.trailer.flatten())
        super()._populate_trailer(trailer)
        trailer[pdf_name('/Prev')] = generic.NumberObject(
            self.prev.last_startxref
        )
        if self.prev.encrypted:
            if self._encrypt is not None:
                trailer[pdf_name("/Encrypt")] = self._encrypt
            else:
                # removing encryption in an incremental update is impossible
                raise ValueError(
                    'Cannot save this document unencrypted. Please call '
                    'encrypt() with the user password of the original file '
                    'before calling write().'
                )

    def write(self, stream):

        if not self.objects:
            # just write the original and then bail
            self._write_header(stream)
            return
        super().write(stream)

    def write_in_place(self):
        stream = self.prev.stream
        stream.seek(0, os.SEEK_END)
        self._write(stream, skip_header=True)

    def encrypt(self, user_pwd):
        prev = self.prev
        # first, attempt decryption
        if prev.encrypted:
            if not prev.decrypt(user_pwd):
                raise ValueError(
                    'Failed to decrypt original with the password supplied'
                )
        else:
            raise ValueError('Original file was not encrypted.')

        # take care to use the same encryption algorithm as the underlying file
        try:
            encrypt_ref = prev.trailer.raw_get("/Encrypt")
        except KeyError:
            raise ValueError(
                'Original document does not have an encryption dictionary'
            )

        self._encrypt_key = self.prev._decryption_key
        self._encrypt = encrypt_ref

    # TODO these can be simplified considerably using the new update_container
    # TODO move these to the base writer class, perhaps

    def add_content_to_page(self, page_ix, pdf_content: PdfContent,
                            prepend=False):
        as_stream = generic.StreamObject({}, stream_data=pdf_content.render())
        return self.add_stream_to_page(
            page_ix, self.add_object(as_stream),
            resources=pdf_content.resources.as_pdf_object(), prepend=prepend
        )

    def add_stream_to_page(self, page_ix, stream_ref, resources=None,
                           prepend=False):
        """
        Append an indirect stream object to a page in a PDF.
        Returns a reference to the page object that was modified.
        """

        page_obj_ref, res_ref = self.find_page_for_modification(page_ix)

        page_obj = page_obj_ref.get_object()

        contents_ref = page_obj.raw_get('/Contents')

        if isinstance(contents_ref, generic.IndirectObject):
            contents = contents_ref.get_object()
            if isinstance(contents, generic.ArrayObject):
                # This is the easy case. It suffices to mark
                # this array for an update, and append our stream to it.
                self.mark_update(contents_ref)
                if prepend:
                    contents.insert(0, stream_ref)
                else:
                    contents.append(stream_ref)
            elif isinstance(contents, generic.StreamObject):
                # replace the old stream with an array containing
                # a reference to the original stream, and our own stream.
                new = [stream_ref, contents_ref] \
                    if prepend else [contents_ref, stream_ref]
                contents = generic.ArrayObject(new)
                page_obj[pdf_name('/Contents')] = self.add_object(contents)
                # mark the page to be updated as well
                self.mark_update(page_obj_ref)
            else:
                raise ValueError('Unexpected type for page /Contents')
        elif isinstance(contents_ref, generic.ArrayObject):
            # make /Contents an indirect array, and append our stream
            contents = contents_ref
            if prepend:
                contents.insert(0, stream_ref)
            else:
                contents.append(stream_ref)
            page_obj[pdf_name('/Contents')] = self.add_object(contents)
            self.mark_update(page_obj_ref)
        else:
            raise ValueError('Unexpected type for page /Contents')

        if resources is None:
            return

        if isinstance(res_ref, generic.IndirectObject):
            # we can get away with only updating this reference
            orig_resource_dict = res_ref.get_object()
            assert isinstance(orig_resource_dict, generic.DictionaryObject)
            if self.merge_resources(orig_resource_dict, resources):
                self.mark_update(res_ref)
        else:
            # don't bother trying to update the resource object, just
            # clone it and add it to the current page object.
            orig_resource_dict = generic.DictionaryObject(res_ref)
            page_obj[pdf_name('/Resources')] = self.add_object(
                orig_resource_dict
            )
            self.merge_resources(orig_resource_dict, resources)

        return page_obj_ref

    def merge_resources(self, orig_dict, new_dict) -> bool:
        """
        Update an existing resource dictionary object with data from another
        one. Returns `True` if the original dict object was modified directly.

        The caller is responsible for avoiding name conflicts with existing
        resources.
        """

        update_needed = False
        for key, value in new_dict.items():
            try:
                orig_value_ref = orig_dict.raw_get(key)
            except KeyError:
                update_needed = True
                orig_dict[key] = value
                continue

            if isinstance(orig_value_ref, generic.IndirectObject):
                orig_value = orig_value_ref.get_object()
                self.mark_update(orig_value_ref)
            else:
                orig_value = orig_value_ref
                update_needed = True

            if isinstance(orig_value, generic.ArrayObject):
                # the /ProcSet case
                orig_value.extend(value)
            elif isinstance(orig_value, generic.DictionaryObject):
                for key_, value_ in value.items():
                    if key_ in orig_value:
                        raise ValueError(
                            'Naming conflict in resource of type %s: '
                            'key %s occurs in both.' % (key, key_)
                        )
                    orig_value[key_] = value_

        return update_needed
