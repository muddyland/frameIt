FROM python:3.14-slim
RUN mkdir /frameit /data /config 
WORKDIR /frameit
COPY . .
RUN pip3 install -r requirements.txt
EXPOSE 5000
CMD ["python","-m", "gunicorn", "-w", "2", "-b", ":5000", "main:app"]
