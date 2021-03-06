import os
import shutil
import tempfile
import uuid
from collections import defaultdict, namedtuple

from mozlog import structuredlog

import manifestupdate
import testloader
import wptmanifest
import wpttest
from expected import expected_path
from vcs import git
manifest = None  # Module that will be imported relative to test_root
manifestitem = None

logger = structuredlog.StructuredLogger("web-platform-tests")


TestItem = namedtuple("TestItem", ["test_manifest", "expected"])

try:
    import ujson as json
except ImportError:
    import json


def load_test_manifests(serve_root, test_paths):
    do_delayed_imports(serve_root)
    manifest_loader = testloader.ManifestLoader(test_paths, False)
    return manifest_loader.load()


def update_expected(test_paths, serve_root, log_file_names,
                    rev_old=None, rev_new="HEAD", ignore_existing=False,
                    sync_root=None, property_order=None, boolean_properties=None,
                    stability=None):
    """Update the metadata files for web-platform-tests based on
    the results obtained in a previous run or runs

    If stability is not None, assume log_file_names refers to logs from repeated
    test jobs, disable tests that don't behave as expected on all runs"""

    manifests = load_test_manifests(serve_root, test_paths)

    id_test_map = update_from_logs(manifests,
                                   *log_file_names,
                                   ignore_existing=ignore_existing,
                                   property_order=property_order,
                                   boolean_properties=boolean_properties,
                                   stability=stability)

    by_test_manifest = defaultdict(list)
    while id_test_map:
        item = id_test_map.popitem()[1]
        by_test_manifest[item.test_manifest].append(item.expected)

    for test_manifest, expected in by_test_manifest.iteritems():
        metadata_path = manifests[test_manifest]["metadata_path"]
        write_changes(metadata_path, expected)
        if stability is not None:
            for tree in expected:
                if not tree.modified:
                    continue
                for test in expected.iterchildren():
                    for subtest in test.iterchildren():
                        if subtest.new_disabled:
                            print "disabled: %s" % os.path.dirname(subtest.root.test_path) + "/" + subtest.name
                        if test.new_disabled:
                            print "disabled: %s" % test.root.test_path

    return by_test_manifest


def do_delayed_imports(serve_root):
    global manifest, manifestitem
    from manifest import manifest, item as manifestitem


def files_in_repo(repo_root):
    return git("ls-tree", "-r", "--name-only", "HEAD").split("\n")


def rev_range(rev_old, rev_new, symmetric=False):
    joiner = ".." if not symmetric else "..."
    return "".join([rev_old, joiner, rev_new])


def paths_changed(rev_old, rev_new, repo):
    data = git("diff", "--name-status", rev_range(rev_old, rev_new), repo=repo)
    lines = [tuple(item.strip() for item in line.strip().split("\t", 1))
             for line in data.split("\n") if line.strip()]
    output = set(lines)
    return output


def load_change_data(rev_old, rev_new, repo):
    changes = paths_changed(rev_old, rev_new, repo)
    rv = {}
    status_keys = {"M": "modified",
                   "A": "new",
                   "D": "deleted"}
    # TODO: deal with renames
    for item in changes:
        rv[item[1]] = status_keys[item[0]]
    return rv


def unexpected_changes(manifests, change_data, files_changed):
    files_changed = set(files_changed)

    root_manifest = None
    for manifest, paths in manifests.iteritems():
        if paths["url_base"] == "/":
            root_manifest = manifest
            break
    else:
        return []

    return [fn for _, fn, _ in root_manifest if fn in files_changed and change_data.get(fn) != "M"]

# For each testrun
# Load all files and scan for the suite_start entry
# Build a hash of filename: properties
# For each different set of properties, gather all chunks
# For each chunk in the set of chunks, go through all tests
# for each test, make a map of {conditionals: [(platform, new_value)]}
# Repeat for each platform
# For each test in the list of tests:
#   for each conditional:
#      If all the new values match (or there aren't any) retain that conditional
#      If any new values mismatch:
#           If stability and any repeated values don't match, disable the test
#           else mark the test as needing human attention
#   Check if all the RHS values are the same; if so collapse the conditionals


def update_from_logs(manifests, *log_filenames, **kwargs):
    ignore_existing = kwargs.get("ignore_existing", False)
    property_order = kwargs.get("property_order")
    boolean_properties = kwargs.get("boolean_properties")
    stability = kwargs.get("stability")

    id_test_map = {}

    for test_manifest, paths in manifests.iteritems():
        id_test_map.update(create_test_tree(
            paths["metadata_path"],
            test_manifest,
            property_order=property_order,
            boolean_properties=boolean_properties))

    updater = ExpectedUpdater(manifests,
                              id_test_map,
                              ignore_existing=ignore_existing)
    for log_filename in log_filenames:
        with open(log_filename) as f:
            updater.update_from_log(f)
    return coalesce_results(id_test_map, stability)


def coalesce_results(id_test_map, stability):
    for _, expected in id_test_map.itervalues():
        if not expected.modified:
            continue
        expected.coalesce_properties(stability=stability)
        for test in expected.iterchildren():
            for subtest in test.iterchildren():
                subtest.coalesce_properties(stability=stability)
            test.coalesce_properties(stability=stability)

    return id_test_map


def directory_manifests(metadata_path):
    rv = []
    for dirpath, dirname, filenames in os.walk(metadata_path):
        if "__dir__.ini" in filenames:
            rel_path = os.path.relpath(dirpath, metadata_path)
            rv.append(os.path.join(rel_path, "__dir__.ini"))
    return rv


def write_changes(metadata_path, expected):
    # First write the new manifest files to a temporary directory
    temp_path = tempfile.mkdtemp(dir=os.path.split(metadata_path)[0])
    write_new_expected(temp_path, expected)

    # Copy all files in the root to the temporary location since
    # these cannot be ini files
    keep_files = [item for item in os.listdir(metadata_path) if
                  not os.path.isdir(os.path.join(metadata_path, item))]

    for item in keep_files:
        dest_dir = os.path.dirname(os.path.join(temp_path, item))
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir)
        shutil.copyfile(os.path.join(metadata_path, item),
                        os.path.join(temp_path, item))

    # Then move the old manifest files to a new location
    temp_path_2 = metadata_path + str(uuid.uuid4())
    os.rename(metadata_path, temp_path_2)
    # Move the new files to the destination location and remove the old files
    os.rename(temp_path, metadata_path)
    shutil.rmtree(temp_path_2)


def write_new_expected(metadata_path, expected):
    # Serialize the data back to a file
    for tree in expected:
        if not tree.is_empty:
            manifest_str = wptmanifest.serialize(tree.node, skip_empty_data=True)
            assert manifest_str != ""
            path = expected_path(metadata_path, tree.test_path)
            dir = os.path.split(path)[0]
            if not os.path.exists(dir):
                os.makedirs(dir)
            with open(path, "wb") as f:
                f.write(manifest_str)


class ExpectedUpdater(object):
    def __init__(self, test_manifests, id_test_map, ignore_existing=False):
        self.id_test_map = id_test_map
        self.ignore_existing = ignore_existing
        self.run_info = None
        self.action_map = {"suite_start": self.suite_start,
                           "test_start": self.test_start,
                           "test_status": self.test_status,
                           "test_end": self.test_end,
                           "assertion_count": self.assertion_count,
                           "lsan_leak": self.lsan_leak}
        self.tests_visited = {}

        self.test_cache = {}

        self.types_by_path = {}
        for manifest in test_manifests.iterkeys():
            for test_type, path, _ in manifest:
                if test_type in wpttest.manifest_test_cls:
                    self.types_by_path[path] = wpttest.manifest_test_cls[test_type]

    def update_from_log(self, log_file):
        self.run_info = None
        action_map = self.action_map
        for line in log_file:
            data = json.loads(line)
            action = data["action"]
            if action in action_map:
                action_map[action](data)

    def suite_start(self, data):
        self.run_info = data["run_info"]

    def test_start(self, data):
        test_id = data["test"]
        try:
            expected_node = self.id_test_map[test_id].expected.get_test(test_id)
        except KeyError:
            print "Test not found %s, skipping" % test_id
            return
        self.test_cache[test_id] = expected_node

        if test_id not in self.tests_visited:
            if self.ignore_existing:
                expected_node.clear("expected")
            self.tests_visited[test_id] = set()

    def test_status(self, data):
        test_id = data["test"]
        test = self.test_cache.get(test_id)
        if test is None:
            return
        test_cls = self.types_by_path[test.root.test_path]

        subtest = test.get_subtest(data["subtest"])

        self.tests_visited[test_id].add(data["subtest"])

        result = test_cls.subtest_result_cls(
            data["subtest"],
            data["status"],
            None)

        subtest.set_result(self.run_info, result)

    def test_end(self, data):
        test_id = data["test"]
        test = self.test_cache.get(test_id)
        if test is None:
            return
        test_cls = self.types_by_path[test.root.test_path]

        if data["status"] == "SKIP":
            return

        result = test_cls.result_cls(
            data["status"],
            None)
        test.set_result(self.run_info, result)
        del self.test_cache[test_id]

    def assertion_count(self, data):
        test_id = data["test"]
        test = self.test_cache.get(test_id)
        if test is None:
            return

        test.set_asserts(self.run_info, data["count"])

    def lsan_leak(self, data):
        dir_path = data.get("scope", "/")
        dir_id = os.path.join(dir_path, "__dir__").replace(os.path.sep, "/")
        if dir_id.startswith("/"):
            dir_id = dir_id[1:]
        expected_node = self.id_test_map[dir_id].expected

        expected_node.set_lsan(self.run_info, (data["frames"], data.get("allowed_match")))


def create_test_tree(metadata_path, test_manifest, property_order=None,
                     boolean_properties=None):
    """Create a map of expectation manifests for all tests in test_manifest,
    reading existing manifests under manifest_path

    :returns: A map of test_id to (manifest, test, expectation_data)
    """
    id_test_map = {}
    exclude_types = frozenset(["stub", "helper", "manual", "support", "conformancechecker"])
    all_types = [item.item_type for item in manifestitem.__dict__.itervalues()
                 if type(item) == type and
                 issubclass(item, manifestitem.ManifestItem) and
                 item.item_type is not None]
    include_types = set(all_types) - exclude_types
    for _, test_path, tests in test_manifest.itertypes(*include_types):
        expected_data = load_or_create_expected(test_manifest, metadata_path, test_path, tests,
                                                property_order, boolean_properties)
        for test in tests:
            id_test_map[test.id] = TestItem(test_manifest, expected_data)

        dir_path = os.path.split(test_path)[0].replace(os.path.sep, "/")
        while True:
            if dir_path:
                dir_id = dir_path + "/__dir__"
            else:
                dir_id = "__dir__"
            dir_id = (test_manifest.url_base + dir_id).lstrip("/")
            if dir_id not in id_test_map:
                expected_data = load_or_create_expected(test_manifest,
                                                        metadata_path,
                                                        dir_id,
                                                        [],
                                                        property_order,
                                                        boolean_properties)

                id_test_map[dir_id] = TestItem(test_manifest, expected_data)
            if not dir_path:
                break
            dir_path = dir_path.rsplit("/", 1)[0] if "/" in dir_path else ""

    return id_test_map


def load_or_create_expected(test_manifest, metadata_path, test_path, tests, property_order=None,
                            boolean_properties=None):
    expected_data = load_expected(test_manifest, metadata_path, test_path, tests,
                                  property_order=property_order,
                                  boolean_properties=boolean_properties)
    if expected_data is None:
        expected_data = create_expected(test_manifest,
                                        test_path,
                                        tests,
                                        property_order=property_order,
                                        boolean_properties=boolean_properties)
    return expected_data


def create_expected(test_manifest, test_path, tests, property_order=None,
                    boolean_properties=None):
    expected = manifestupdate.ExpectedManifest(None, test_path, test_manifest.url_base,
                                               property_order=property_order,
                                               boolean_properties=boolean_properties)
    for test in tests:
        expected.append(manifestupdate.TestNode.create(test.id))
    return expected


def load_expected(test_manifest, metadata_path, test_path, tests, property_order=None,
                  boolean_properties=None):
    expected_manifest = manifestupdate.get_manifest(metadata_path,
                                                    test_path,
                                                    test_manifest.url_base,
                                                    property_order=property_order,
                                                    boolean_properties=boolean_properties)
    if expected_manifest is None:
        return

    tests_by_id = {item.id: item for item in tests}

    # Remove expected data for tests that no longer exist
    for test in expected_manifest.iterchildren():
        if test.id not in tests_by_id:
            test.remove()

    # Add tests that don't have expected data
    for test in tests:
        if not expected_manifest.has_test(test.id):
            expected_manifest.append(manifestupdate.TestNode.create(test.id))

    return expected_manifest
