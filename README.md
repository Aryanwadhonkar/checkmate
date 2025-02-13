# 🏥 AI Health Assistant Chatbot  

## 📌 Overview  
The **AI Health Assistant Chatbot** provides users with medical information, including:  
- **Disease Explanation**: Gives a detailed understanding of various diseases.  
- **Symptoms Identification**: Lists symptoms based on user queries.  
- **Treatment Information**: Suggests possible treatments and medications.  
- **Precautionary Measures**: Provides steps to prevent or manage diseases effectively.  

This chatbot is built using **LangChain**, **Hugging Face Models**, and **FAISS** for efficient retrieval-based AI responses. It features a **Streamlit UI** for easy interaction.  

---

## 🚀 Features  
✅ **AI-powered Q&A**: Retrieves accurate health-related answers.  
✅ **Fast and Efficient**: Uses FAISS for optimized response retrieval.  
✅ **User-friendly Interface**: Built with Streamlit for smooth interaction.  
✅ **API Integration**: Uses Hugging Face for model hosting.  
✅ **Scalable & Customizable**: Can be expanded with more datasets and models.  

---

## 🛠️ Installation  

Ensure **Python 3.8+** is installed. Then, install the required dependencies:  

```bash
pip install langchain langchain_community langchain_huggingface faiss-cpu pypdf
pip install huggingface_hub
pip install streamlit
```

## ⚙️ Installation & Setup
To run the AI Health Assistant Chatbot, follow these steps:

### 📥 Prerequisites
Ensure you have the following installed:
- Python (>=3.8)
- pip
- Virtual environment (optional but recommended)

### 🚀 Running the Chatbot
1. Clone the repository:
```sh
git clone https://github.com/ryuk27/checkmate.git
cd checkmate
```
2. Run the chatbot:
```sh
streamlit run medibot.py
```

## 📜 Usage
- Open the chatbot interface in your browser.
- Enter queries related to diseases, symptoms, treatments, or precautions.
- Receive AI-generated responses based on medical knowledge.
