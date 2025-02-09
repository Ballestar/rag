from llama_index import VectorStoreIndex
from llama_index.vector_stores import PineconeVectorStore
import logging
import os
from datetime import datetime
import pinecone
from pathlib import Path

from src.Llama_index_sandbox.data_ingestion_youtube.load.load import load_video_transcripts
from src.Llama_index_sandbox import PDF_DIRECTORY, YOUTUBE_VIDEO_DIRECTORY, config
import src.Llama_index_sandbox.data_ingestion_pdf.load as load_pdf
import src.Llama_index_sandbox.data_ingestion_pdf.chunk as chunk_pdf
import src.Llama_index_sandbox.data_ingestion_youtube.chunk as chunk_youtube
import src.Llama_index_sandbox.embed as embed
from src.Llama_index_sandbox import index_dir
from src.Llama_index_sandbox.utils import timeit

api_key = os.environ["PINECONE_API_KEY"]


@timeit
def initialise_vector_store(embedding_model_vector_dimension) -> PineconeVectorStore:
    pinecone.init(api_key=api_key, environment=os.environ["PINECONE_API_ENVIRONMENT"])
    index_name = "quickstart"

    # Check if the index already exists
    existing_indexes = pinecone.list_indexes()
    if index_name in existing_indexes:
        # If the index exists, delete it
        pinecone.delete_index(index_name)

    # NOTE: We do not index the metadata fields by video/paper link.
    #  https://docs.pinecone.io/docs/manage-indexes#selective-metadata-indexing
    #  https://docs.pinecone.io/docs/metadata-filtering
    # High cardinality consumes more memory: Pinecone indexes metadata to allow
    # for filtering. If the metadata contains many unique values — such as a unique
    # identifier for each vector — the index will consume significantly more
    # memory. Consider using selective metadata indexing to avoid indexing
    # high-cardinality metadata that is not needed for filtering.
    metadata_config = {
        "indexed": ["document_type", "title", "authors", "release_date"]
    }
    pinecone.create_index(name=index_name,
                          metadata_config=metadata_config,
                          dimension=embedding_model_vector_dimension,
                          metric="cosine",
                          pod_type="p1")
    pinecone_index = pinecone.Index(index_name=index_name)
    vector_store = PineconeVectorStore(pinecone_index=pinecone_index)

    # Optionally, you might want to delete all contents in the index
    # pinecone_index.delete(deleteAll=True)
    return vector_store


@timeit
def persist_index(index, embedding_model_name, TEXT_SPLITTER_CHUNK_SIZE, TEXT_SPLITTER_CHUNK_OVERLAP_PERCENTAGE):
    """
    Persist the index to disk.
    NOTE: Given that we use an external DB, this only writes a json containing the ID referring to that DB.
    """
    try:
        # Format the filename
        if '/' in embedding_model_name:
            embedding_model_name = embedding_model_name.split('/')[-1]
        date_str = datetime.now().strftime("%Y-%m-%d-%H-%M")
        name = f"{date_str}_{embedding_model_name}_{TEXT_SPLITTER_CHUNK_SIZE}_{TEXT_SPLITTER_CHUNK_OVERLAP_PERCENTAGE}"
        persist_dir = index_dir + name
        # check if index_dir and if not create it
        if not os.path.exists(index_dir):
            os.makedirs(index_dir)
        # NOTE 2023-09-29: https://stackoverflow.com/questions/76837143/llamaindex-index-storage-context-persist-not-storing-vector-store
        #   Vector Store IS NOT persisted. The method index.storage_context.persist is failing silently since when attempting to
        #   load the index back, it fails since there is no vector json file
        index.storage_context.persist(persist_dir=persist_dir)
        # create a vector_store.json file with {} inside
        with open(f"{persist_dir}/vector_store.json", "w") as f:
            f.write("{}")

        logging.info(f"Successfully persisted index {persist_dir} to disk.")
    except Exception as e:
        logging.error(f"Failed to persist index to disk. Error: {e}")


@timeit
def load_nodes_into_vector_store_create_index(nodes, embedding_model_vector_dimension) -> VectorStoreIndex:
    """
    We now insert these nodes into our PineconeVectorStore.

    NOTE: We skip the VectorStoreIndex abstraction, which is a higher-level
    abstraction that handles ingestion as well. We use VectorStoreIndex in the next section to fast-track retrieval/querying.
    """
    vector_store = initialise_vector_store(embedding_model_vector_dimension=embedding_model_vector_dimension)
    vector_store.add(nodes)
    index = VectorStoreIndex.from_vector_store(vector_store)
    return index


@timeit
def load_index_from_disk(service_context) -> VectorStoreIndex:
    # load the latest directory in index_dir
    persist_dir = f"{index_dir}{sorted(os.listdir(index_dir))[-1]}"
    logging.info(f"LOADING INDEX {persist_dir} FROM DISK")
    try:
        pinecone.init(api_key=api_key, environment=os.environ["PINECONE_API_ENVIRONMENT"])
        index_name = "quickstart"
        vector_store = PineconeVectorStore(pinecone_index=pinecone.Index(index_name))
        index = VectorStoreIndex.from_vector_store(vector_store, service_context)
        return index
    except Exception as e:
        logging.error(f"Error: {e}")
        # To accommodate for the case where the vector_store.json file is not persisted https://stackoverflow.com/questions/76837143/llamaindex-index-storage-context-persist-not-storing-vector-store
        if "No existing llama_index.vector_stores.simple" in str(e):
            # create a vector_store.json file with {} inside
            with open(f"{persist_dir}/vector_store.json", "w") as f:
                f.write("{}")
            try:
                pinecone.init(api_key=api_key, environment=os.environ["PINECONE_API_ENVIRONMENT"])
                index_name = "quickstart"
                vector_store = PineconeVectorStore(pinecone_index=pinecone.Index(index_name))
                index = VectorStoreIndex.from_vector_store(vector_store, service_context)
                return index
            except Exception as e:
                logging.error(f"load_index_from_disk ERROR: {e}")
                exit(1)


@timeit
def create_index(embedding_model_name, embedding_model, TEXT_SPLITTER_CHUNK_SIZE, TEXT_SPLITTER_CHUNK_OVERLAP_PERCENTAGE, add_new_transcripts, num_files=None):
    logging.info("RECREATING INDEX")
    # 1. Data loading
    # pdf_links, save_dir = fetch_pdf_list(num_papers=None)
    # download_pdfs(pdf_links, save_dir)
    documents_pdfs = load_pdf.load_pdfs(directory_path=Path(PDF_DIRECTORY), num_files=num_files)
    documents_youtube = load_video_transcripts(directory_path=Path(YOUTUBE_VIDEO_DIRECTORY), add_new_transcripts=add_new_transcripts, num_files=num_files)

    # 2. Data chunking / text splitter
    text_chunks_pdfs, doc_idxs_pdfs = chunk_pdf.chunk_documents(documents_pdfs, TEXT_SPLITTER_CHUNK_OVERLAP_PERCENTAGE=TEXT_SPLITTER_CHUNK_OVERLAP_PERCENTAGE, TEXT_SPLITTER_CHUNK_SIZE=TEXT_SPLITTER_CHUNK_SIZE)
    text_chunks_youtube, doc_idxs_youtube = chunk_youtube.chunk_documents(documents_youtube, TEXT_SPLITTER_CHUNK_SIZE=TEXT_SPLITTER_CHUNK_SIZE, TEXT_SPLITTER_CHUNK_OVERLAP_PERCENTAGE=TEXT_SPLITTER_CHUNK_OVERLAP_PERCENTAGE)

    # 3. Manually Construct Nodes from Text Chunks
    nodes_pdf = embed.construct_node(text_chunks_pdfs, documents_pdfs, doc_idxs_pdfs)
    nodes_youtube = embed.construct_node(text_chunks_youtube, documents_youtube, doc_idxs_youtube)

    # [Optional] 4. Extract Metadata from each Node by performing LLM calls to fetch Title.
    #        We extract metadata from each Node using our Metadata extractors.
    #        This will add more metadata to each Node.
    # nodes = enrich_nodes_with_metadata_via_llm(nodes)

    # 5. Generate Embeddings for each Node
    embed.generate_embeddings(nodes_pdf, embedding_model)
    embed.generate_embeddings(nodes_youtube, embedding_model)
    nodes = nodes_pdf + nodes_youtube

    # 6. Load Nodes into a Vector Store
    # We now insert these nodes into our PineconeVectorStore.
    # NOTE: We skip the VectorStoreIndex abstraction, which is a higher-level abstraction
    # that handles ingestion as well. We use VectorStoreIndex in the next section to fast-trak retrieval/querying.
    index = load_nodes_into_vector_store_create_index(nodes, embedding_model_vector_dimension=config.EMBEDDING_DIMENSIONS[embedding_model_name])
    persist_index(index, embedding_model_name, TEXT_SPLITTER_CHUNK_SIZE=TEXT_SPLITTER_CHUNK_SIZE, TEXT_SPLITTER_CHUNK_OVERLAP_PERCENTAGE=TEXT_SPLITTER_CHUNK_OVERLAP_PERCENTAGE)
    return index
