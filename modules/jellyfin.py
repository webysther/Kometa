from __future__ import annotations

import os
import types
from types import SimpleNamespace
from typing import Any

import jellyfin
from jellyfin.generated import (
    BaseItemDto,
    BaseItemKind,
    CollectionApi,
    CollectionType,
    ImageType,
    ItemFields,
    LibraryApi,
)
from jellyfin.items import Item
from plexapi.video import Movie

from modules import util
from modules.library import Library
from modules.poster import ImageData
from modules.util import Failed

logger = util.logger


class Jellyfin(Library):
    def __init__(self, config, params):
        """Initializes Jellyfin Object

        Args:
            config (modules.config.ConfigFile): Config object
            params (dict): Dictionary of Jellyfin parameters
        """
        # TODO: https://github.com/Kometa-Team/Kometa/pull/2788
        params["plex"] = {}
        params["plex"]["optimize"] = None
        params["plex"]["clean_bundles"] = None
        params["plex"]["empty_trash"] = None

        super().__init__(config, params)

        # TODO: https://github.com/Kometa-Team/Kometa/issues/2791
        self.config.Cache.query_image_map = types.MethodType(
            lambda self, rating_key, table_name: (None, None, None), self.config.Cache
        )

        self.config.Cache.update_image_map = types.MethodType(
            lambda self, rating_key, table_name, location, compare, overlay=None: None,
            self.config.Cache,
        )

        self.jellyfin = params["jellyfin"]
        self.url = self.jellyfin["url"]
        self.session = self.config.Requests.session

        # not used by jellyfin
        self.clean_bundles = False
        self.empty_trash = False
        self.optimize = False

        # the builder make reference to Plex directly so we set it here
        self.Plex = PlexWrapper(self)

        # jellyfin does not have a language setting per library
        self.language = "en"

        if (
            self.jellyfin["verify_ssl"] is False
            and self.config.Requests.global_ssl is True
        ):
            logger.debug("Overriding verify_ssl to False for Jellyfin connection")
            self.session = self.config.Requests.create_session(verify_ssl=False)
        if (
            self.jellyfin["verify_ssl"] is True
            and self.config.Requests.global_ssl is False
        ):
            logger.debug("Overriding verify_ssl to True for Jellyfin connection")
            self.session = self.config.Requests.create_session()

        self.token = self.jellyfin["token"]
        self.timeout = self.jellyfin["timeout"]

        self.api = jellyfin.api(self.url, self.token)

        # we register a client to identify ourselves to the server
        self.api.register_client(client_name="Kometa")

        # some api methods require a user context, so we store the user here
        self.api.user = self.jellyfin["user"]

        # we also wrap the server object to match Plex server interface
        self.PlexServer = ServerWrapper(self.api.system)

        logger.info(self.url)
        logger.secret(self.token)
        try:
            self._server_version = self.api.system.info.version
            self._server_name = self.api.system.info.server_name

            logger.info(
                "Connected to server %s version %s",
                self._server_name,
                self._server_version,
            )
        except Exception as e:
            logger.info("Jellyfin Error: Jellyfin connection attempt failed")
            logger.stacktrace()
            raise Failed(f"Jellyfin Error: {e}")

        candidates = (
            self.api.items.search.only_library().add("name_starts_with", self.name).all
        )
        libraries = [lib for lib in candidates if lib.name == self.name]

        if len(libraries) == 0:
            raise Failed(f"Jellyfin Library '{params['name']}' not found.")

        if len(libraries) > 1:
            names = [f"'{lib.name}'" for lib in libraries]
            raise Failed(
                f"Jellyfin Library '{params['name']}' is not unique. Options: {names}"
            )

        item = libraries[0]
        self.Jellyfin = item
        self.type = item.collection_type.value

        if item.collection_type == CollectionType.MOVIES:
            self.type = "Movie"

        self.is_movie = self.type == "Movie"
        self.is_show = self.type == "TVShow"
        self.is_music = self.type == "Music"
        self.is_playlist = self.type == "Playlist"
        self.is_other = self.type == "Boxset"

        self._all_items = []

    def notify(
        self, text: str, collection: str | None = None, critical: bool = True
    ) -> None:
        """Sends a notification to the server.

        Args:
            text (str): The text of the notification.
            collection (str | None): The collection the notification
                is for. Defaults to None.
            critical (bool): Whether the notification is critical.
                Defaults to True.
        """
        self.config.notify(
            text,
            server=self._server_name,
            library=self.name,
            collection=collection,
            critical=critical,
        )

    def get_all(
        self, builder_level: str | None = None, load: bool = False
    ) -> list[ItemMovieWrapper]:
        """Returns all items in the library.

        Args:
            builder_level (str | None): The type of items to return. Defaults to None.
            load (bool): Whether to force a reload of all items. Defaults to False.

        Returns:
            list[ItemMovieWrapper]: A list of all items in the library.
        """
        # cache all items for movie libraries
        if load and builder_level in [None, "movie"]:
            self._all_items = []
        if self._all_items and builder_level in [None, "movie"]:
            return self._all_items
        display_level = self.type

        logger.info(
            f"Loading All {display_level.capitalize()} from Library: {self.name}"
        )

        search = self.api.items.search
        search.include_item_types = [BaseItemKind.MOVIE]
        search.fields = [ItemFields.PROVIDERIDS, ItemFields.PATH]
        search.recursive()
        result = []

        for item in search.all:
            result.append(ItemMovieWrapper(item))

        logger.info(f"Loaded {len(result)} {display_level.capitalize()}")

        if builder_level in [None, "movie"]:
            self._all_items = result
        return result

    def get_all_collections(self, label: str | None = None) -> list[ItemMovieWrapper]:
        """Returns all collections in the library.

        Args:
            label (str | None): The label to filter collections by. Defaults to None.

        Returns:
            list[ItemMovieWrapper]: A list of all collections in the library.
        """
        search = self.api.items.search
        search.include_item_types = [BaseItemKind.BOXSET]

        result = []
        for item in search.all:
            result.append(ItemMovieWrapper(item))
        return result

    def get_collection(
        self, data: str, force_search: bool = False, debug: bool = True
    ) -> ItemMovieWrapper:
        """Returns a collection by name.

        Args:
            data (str): The name of the collection.
            force_search (bool): Whether to force a search for
                the collection. Defaults to False.
            debug (bool): Whether to print debug information.
                Defaults to True.

        Returns:
            ItemMovieWrapper: The collection object.
        """
        search = self.api.items.search
        search.include_item_types = [BaseItemKind.BOXSET]
        collections = search.recursive().all
        for collection in collections:
            if collection.name == data:
                return ItemMovieWrapper(collection)

        item = Item(BaseItemDto())
        item.name = data
        return ItemMovieWrapper(item)

    def split(self, text):
        attribute, modifier = os.path.splitext(str(text).lower())
        final = f"{attribute}{modifier}"
        return attribute, modifier, final

    def fetch_item(self, item):
        key = item
        if key in self.cached_items:
            return self.cached_items[key][0]
        raise Failed(f"Jellyfin Error: Item {item} not found")

    def get_collection_items(
        self,
        collection: str | ItemMovieWrapper,
        smart_label_collection: bool = False,
    ) -> list[ItemMovieWrapper]:
        """Returns the items of a collection.

        Args:
            collection (str | ItemMovieWrapper): The name or
                object of the collection.
            smart_label_collection (bool): Whether the collection
                is a smart label collection.

        Returns:
            list[ItemMovieWrapper]: The items of the collection.
        """
        if smart_label_collection is True:
            warn_msg = "Jellyfin smart label collections are not supported"
            logger.warning(warn_msg)

        name = collection if isinstance(collection, str) else collection.title
        item = self.get_collection(name)

        if item.ratingKey == 0:
            return []

        search = self.api.items.search.recursive()
        search.include_item_types = [BaseItemKind.MOVIE]
        search.parent_id = item.id
        result = []
        for movie in search.all:
            result.append(ItemMovieWrapper(movie))
        return result

    def get_collection_name_and_items(
        self,
        collection: str | ItemMovieWrapper,
        smart_label_collection: bool = False,
    ) -> tuple[str, list[ItemMovieWrapper]]:
        """Returns the name and items of a collection.

        Args:
            collection (str | ItemMovieWrapper): The name or
                object of the collection.
            smart_label_collection (bool): Whether the collection
                is a smart label collection.

        Returns:
            tuple[str, list[ItemMovieWrapper]]: The name and
                items of the collection.
        """
        if smart_label_collection is True:
            warn_msg = "Jellyfin smart label collections are not supported"
            logger.warning(warn_msg)

        name = collection if isinstance(collection, str) else collection.title
        return name, self.get_collection_items(collection, smart_label_collection)

    def alter_collection(
        self,
        items: list[ItemMovieWrapper],
        collection: str,
        smart_label_collection=False,
        add=True,
    ) -> None:
        """Alters a collection by adding or removing items.

        Args:
            items (list[ItemMovieWrapper]): The items to add or remove.
            collection (str): The name of the collection.
            smart_label_collection (bool): Whether the collection
                is a smart label collection. Defaults to False.
            add (bool): Whether to add or remove the items.
                Defaults to True.
        """
        if smart_label_collection is True:
            warn_msg = "Jellyfin smart label collections are not supported"
            logger.warning(warn_msg)

        item = self.get_collection(collection)

        if item.ratingKey == 0:
            item = self.create_collection(item)

        if item.id is None:
            raise Failed(f"Jellyfin Error: Could not create collection {collection}")

        if add:
            self.add_to_collection(items, item)
        else:
            self.remove_from_collection(items, item)

    def create_collection(self, item: ItemMovieWrapper) -> ItemMovieWrapper:
        """Creates a new collection.

        Args:
            item (ItemMovieWrapper): The collection to create.

        Returns:
            ItemMovieWrapper: The created collection object.
        """
        collection = CollectionApi().create_collection(
            name=item.title, parent_id=self.Jellyfin.id
        )

        item.id = collection.id
        return item

    def add_to_collection(
        self, items: list[ItemMovieWrapper], collection: ItemMovieWrapper
    ) -> None:
        """Adds items to a collection.

        Args:
            items (list[ItemMovieWrapper]): The items to add.
            collection (ItemMovieWrapper): The collection to add the items to.
        """
        CollectionApi().add_to_collection(collection.id, [item.id for item in items])

    def remove_from_collection(
        self, items: list[ItemMovieWrapper], collection: ItemMovieWrapper
    ) -> None:
        """Removes items from a collection.

        Args:
            items (list[ItemMovieWrapper]): The items to remove.
            collection (ItemMovieWrapper): The collection to remove the items from.
        """
        CollectionApi().remove_from_collection(
            collection.id, [item.id for item in items]
        )

    def item_reload(self, item):
        if item.type == BaseItemKind.BOXSET:
            return self.get_collection(item.name)
        if item.type == BaseItemKind.MOVIE:
            return ItemMovieWrapper(self.api.items.by_id(item.id))

    def delete(self, obj):
        items = self.api.items.search.recursive().add("name_starts_with", obj.name).all
        for item in items:
            if item.name == obj.name:
                LibraryApi().delete_item(item.id)
                return True
        return False

    def fetchItem(self, data):
        warn_msg = "Jellyfin fetchItem method not implemented yet"
        logger.warning(warn_msg)
        return None

    def fetchItems(self, uri_args: dict | None = None) -> list[ItemMovieWrapper]:
        if uri_args is not None:
            warn_msg = "Jellyfin fetchItems method not implemented yet"
            logger.warning(warn_msg)
            return []

        results = []
        for item in self.api.items.search.recursive().all:
            results.append(ItemMovieWrapper(item))
        return results

    def edit_tags(
        self,
        attr: str,
        obj: ItemMovieWrapper,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
        sync_tags: list[str] | None = None,
        do_print: bool = True,
        locked: bool = True,
        is_locked: bool | None = None,
    ) -> None:
        # Get the user library item
        if obj.id is None:
            return

        model = self.api.items.edit(obj.id)

        if add_tags and model.tags is None:
            model.tags = add_tags
        elif add_tags:
            model.tags.extend(add_tags)
        if remove_tags and model.tags:
            model.tags = [tag for tag in model.tags if tag not in remove_tags]
        if sync_tags is not None:
            model.tags = sync_tags

        if do_print:
            logger.info(model.tags)

        # remove duplicates
        model.tags = list(set(model.tags)) if model.tags else []
        model.lock_data = locked
        model.save()

    def find_poster_url(self, item):
        warn_msg = "Jellyfin find_poster_url method not implemented yet"
        logger.warning(warn_msg)

    def get_rating_keys(self, method, data, is_playlist=False):
        warn_msg = "Jellyfin get_rating_keys method not implemented yet"
        logger.warning(warn_msg)

    def image_update(self, item, image, tmdb=None, title=None, poster=True):
        warn_msg = "Jellyfin image_update method not implemented yet"
        logger.warning(warn_msg)

    def item_labels(self, item):
        pass

    def item_posters(self, item):
        warn_msg = "Jellyfin item_posters method not implemented yet"
        logger.warning(warn_msg)

    def notify_delete(self, message_id):
        warn_msg = "Jellyfin notify_delete method not implemented yet"
        logger.warning(warn_msg)

    def reload(self, item, force=False):
        if force:
            return self.fetch_item(item.id)
        return self.item_reload(item)

    def upload_poster(self, item, image, tmdb=None, title=None):
        warn_msg = "Jellyfin upload_poster method not implemented yet"
        logger.warning(warn_msg)

    def collection_order_query(self, collection, data):
        warn_msg = "Jellyfin dont support collection order"
        logger.warning(warn_msg)

    def find_item_assets(
        self, item, item_asset_directory=None, asset_directory=None, folder_name=None
    ):
        warn_msg = "Jellyfin find_item_assets method not implemented yet"
        logger.warning(warn_msg)
        return None, None, None, item_asset_directory, folder_name

    def _upload_image(self, item: ItemMovieWrapper, image: ImageData) -> bool:
        if image.is_poster:
            image_type = ImageType.PRIMARY
        elif image.is_background:
            image_type = ImageType.BACKDROP
        elif image.is_logo:
            image_type = ImageType.LOGO
        else:
            return False

        return self.api.image.upload_from_url(item.id, image_type, image.location)

    def playlist_report(self):
        warn_msg = "Jellyfin playlist_report method not implemented yet"
        logger.warning(warn_msg)
        return {}

    def moveItem(self, obj, item, after):
        warn_msg = "Jellyfin moveItem method not implemented yet"
        logger.warning(warn_msg)

    def smart_label_check(self, name):
        return False

    def search(self, title=None, sort=None, maxresults=None, libtype=None, **kwargs):
        items = self.get_all()
        label = kwargs.get("label")
        if label:
            items = [
                i
                for i in items
                if hasattr(i, "item")
                and hasattr(i.item, "tags")
                and i.item.tags
                and label in i.item.tags
            ]
        if title:
            items = [i for i in items if i.title == title]
        return items

    def collection_visibility(self, collection):
        return {"library": False, "home": False, "shared": False}

    def collection_visibility_update(
        self, collection, visibility=None, library=None, home=None, shared=None
    ):
        logger.debug("Jellyfin does not support Plex hub visibility settings")

    def get_ids(self, item):
        tmdb_id = None
        tvdb_id = None
        imdb_id = None
        provider_ids = getattr(item, "item", item)
        if hasattr(provider_ids, "provider_ids") and provider_ids.provider_ids:
            pids = provider_ids.provider_ids
            if "Tmdb" in pids and pids["Tmdb"]:
                try:
                    tmdb_id = int(pids["Tmdb"])
                except (ValueError, TypeError):
                    pass
            if "Tvdb" in pids and pids["Tvdb"]:
                try:
                    tvdb_id = int(pids["Tvdb"])
                except (ValueError, TypeError):
                    pass
            if "Imdb" in pids and pids["Imdb"]:
                imdb_id = str(pids["Imdb"])
        return tmdb_id, tvdb_id, imdb_id

    def get_ratings(self, item):
        return {}

    def get_item_display_title(self, item, sort=False):
        return item.titleSort if sort else item.title

    def get_locked_attributes(
        self, item, titles=None, year_titles=None, item_type=None
    ):
        return {}

    def get_search_choices(self, search_name, title=True, name_pairs=False):
        logger.warning("Jellyfin get_search_choices not implemented yet")
        return []

    def get_playlist(self, title):
        raise Failed("Jellyfin Error: Playlists are not supported yet")

    def get_playlist_from_users(self, title):
        raise Failed("Jellyfin Error: Playlists are not supported yet")

    def query(self, method, *args, **kwargs):
        logger.warning("Jellyfin does not support Plex query method")
        return None

    def edit_query(self, item, edits, advanced=False):
        logger.warning("Jellyfin does not support Plex edit_query method")

    def create_smart_collection(self, title, smart_type, uri_args):
        logger.warning("Jellyfin does not support smart collections")
        return None

    def update_smart_collection(self, collection, uri_args):
        logger.warning("Jellyfin does not support smart collections")

    def smart_filter(self, collection):
        return None

    def get_smart_filter_from_uri(self, uri):
        return (None, None)


class PlexWrapper:
    def __init__(self, library: Library):
        """Initializes PlexWrapper object"""
        self.library = library

    @property
    def type(self) -> str:
        """Returns the type of the library"""
        # Jellyfin library types are capitalized, run_definition expect lowercase
        return self.library.type.lower()

    def __getattr__(self, name):
        return getattr(self.library, name)


class ServerWrapper:
    """Wrapper for Jellyfin Server to match Plex Server interface"""

    def __init__(self, system):
        """Initializes ServerWrapper object

        Args:
            system (jellyfin.api.system): The Jellyfin system object
        """
        self.system = system

    @property
    def machineIdentifier(self) -> str:
        """Returns the machine identifier of the server"""
        return self.system.info.id

    @property
    def friendlyName(self) -> str:
        """Returns the friendly name of the server"""
        return self.system.info.server_name


class ItemMovieWrapper(Movie):
    """Wrapper for Jellyfin Movie item to match Plex Movie interface"""

    def __init__(self, item: Item):
        """Initializes ItemMovieWrapper object"""
        self.item = item

    @property
    def model(self) -> BaseItemDto:
        """Returns the underlying BaseItemDto model"""
        return self.item.model

    @property
    def smart(self) -> bool:
        """Returns False as Jellyfin does not support smart collections"""
        return False

    @property
    def title(self) -> str:
        """Returns the title of the movie"""
        return self.item.name if self.item.name else ""

    @property
    def summary(self) -> str:
        """Returns the summary of the movie"""
        return self.item.overview if self.item.overview else ""

    @property
    def ratingKey(self) -> int:
        """Returns the rating key of the movie"""
        return self.item.id.int if self.item.id else 0

    @property
    def year(self) -> int:
        """Returns the release year of the movie"""
        return self.item.production_year if self.item.production_year else 0

    @property
    def collectionSort(self) -> int:
        """Returns the collection sort order of the movie"""
        return 0

    @property
    def titleSort(self) -> str:
        """Returns the sort title of the movie"""
        return self.item.sort_name if self.item.sort_name else ""

    def editSummary(self, summary: str) -> None:
        """Edits the summary of the movie"""
        self.item.overview = summary

    def editSortTitle(self, new_sort_title: str) -> None:
        """Edits the sort title of the movie"""
        self.item.sort_name = new_sort_title

    @property
    def guid(self) -> str | None:
        """Returns the GUID of the movie."""
        # Jellyfin does not have a GUID, we construct one from provider IDs if available
        if self.item.provider_ids and "Tmdb" in self.item.provider_ids:
            return f"themoviedb://{self.item.provider_ids['Tmdb']}"
        if self.item.provider_ids and "Imdb" in self.item.provider_ids:
            return f"imdb://{self.item.provider_ids['Imdb']}"
        return None

    @property
    def guids(self) -> list[SimpleNamespace]:
        """Returns a list of GUIDs matching the format expected by convert.py."""
        result: list[SimpleNamespace] = []
        if not self.item.provider_ids:
            return result
        provider_map = {"Tmdb": "tmdb", "Imdb": "imdb", "Tvdb": "tvdb"}
        for key, prefix in provider_map.items():
            if key in self.item.provider_ids:
                result.append(
                    SimpleNamespace(id=f"{prefix}://{self.item.provider_ids[key]}")
                )
        return result

    @property
    def childCount(self) -> int:
        """Returns the child count of the movie"""
        # For movies, child count is typically 0
        # For collections, it would be the number of items in the collection
        # Here we return 0 for movies and actual child count for collections
        return self.item.child_count if self.item.child_count else 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.item, name)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, ItemMovieWrapper):
            return self.ratingKey == other.ratingKey
        return False

    def __hash__(self) -> int:
        return hash(self.ratingKey)

    def __repr__(self) -> str:
        return (
            f"<ItemMovieWrapper\n"
            f"  id={self.item.id}\n"
            f"  ratingKey={self.ratingKey}\n"
            f"  title={self.title}\n>"
        )
