from typing import Union

import demisto_client
from wcmatch.pathlib import Path

from demisto_sdk.commands.common.constants import PARSING_RULE, FileType
from demisto_sdk.commands.common.content.objects.pack_objects.abstract_pack_objects.yaml_unify_content_object import \
    YAMLContentUnifiedObject


class ParsingRule(YAMLContentUnifiedObject):
    def __init__(self, path: Union[Path, str]):
        super().__init__(path, FileType.PARSING_RULE, PARSING_RULE)

    def upload(self, client: demisto_client):
        """
        Upload the parsing_rule to demisto_client
        Args:
            client: The demisto_client object of the desired XSOAR machine to upload to.

        Returns:
            The result of the upload command from demisto_client
        """
        # return client.import_parsing_rules(file=self.path)
        pass

    def type(self):
        return FileType.PARSING_RULE