import unittest


class Example(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.value = 40

    def setUp(self) -> None:
        self.value += 1

    def test_answer(self) -> None:
        assert self.value + 1 == 42

    def tearDown(self) -> None:
        self.value = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls.value = 0
